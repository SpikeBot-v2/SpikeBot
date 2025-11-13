# cogs/valorant_commands.py
import discord
from discord import app_commands
from discord.ext import commands, tasks
import asyncio
import os
import aiofiles
import aiofiles.os
import uuid
import datetime
import base64
import re
from cryptography.fernet import Fernet
from sqlalchemy.future import select
from sqlalchemy import update as sqlalchemy_update, delete, exc

from database.database import async_session
from database.models import State, RiotAccount, DailyStoreSchedule
from api.riot_api import RiotAPI
from image_generator import create_daily_store_image


CLIENT_PLATFORM = base64.b64encode(
    b'{"platformType":"PC","platformOS":"Windows","platformOSVersion":"10.0.19042.1.256.64bit","platformChipset":"Unknown"}'
).decode()

# --- Views for account selection ---

class AccountSelectView(discord.ui.View):
    """
    複数のアカウントから一つを選択させるためのView。
    """
    def __init__(self, accounts: list[RiotAccount], callback_coro, placeholder: str = "アカウントを選択してください..."):
        super().__init__(timeout=180)
        self.callback_coro = callback_coro
        
        options = [
            discord.SelectOption(label=acc.account_name, value=str(acc.id), description=acc.riot_id)
            for acc in accounts
        ]
        self.select_menu = discord.ui.Select(placeholder=placeholder, options=options)
        self.select_menu.callback = self.on_select
        self.add_item(self.select_menu)

    async def on_select(self, interaction: discord.Interaction):
        # 選択されたアカウントIDを取得
        account_id = int(self.select_menu.values[0])
        
        # 元のコマンドに処理を戻す
        await self.callback_coro(interaction, account_id)
        
        # Viewを無効化
        for item in self.children:
            item.disabled = True
        try:
            # Follow-up応答ではメッセージを編集できないことがあるため、元の応答を編集
            await interaction.response.edit_message(view=self)
        except (discord.NotFound, discord.InteractionResponded):
             try:
                # interaction.response.edit_messageが失敗した場合、元のメッセージを取得して編集
                original_response = await interaction.original_response()
                await original_response.edit(view=self)
             except discord.NotFound:
                pass # メッセージが削除されている場合は何もしない

class ValorantCommands(commands.Cog):
    account = app_commands.Group(name="account", description="アカウント関連のコマンド")
    store = app_commands.Group(name="store", description="ストア関連のコマンド")
    schedule = app_commands.Group(name="schedule", description="デイリーストアの自動投稿スケジュール")

    def __init__(self, bot: commands.Bot, your_domain: str, fernet: Fernet):
        self.bot = bot
        self.your_domain = your_domain
        self.fernet = fernet
        self.skin_cache = {}
        self.level_to_skin_map = {}
        self.client_version = None
        self.daily_store_task.start()

    def cog_unload(self):
        self.daily_store_task.cancel()

    @tasks.loop(minutes=1)
    async def daily_store_task(self):
        # JSTの現在時刻を取得
        jst = datetime.timezone(datetime.timedelta(hours=9))
        now_jst = datetime.datetime.now(jst)
        
        # 比較のために秒とマイクロ秒を0に設定
        current_time = now_jst.time().replace(second=0, microsecond=0)

        async with async_session() as session:
            result = await session.execute(
                select(DailyStoreSchedule, RiotAccount.riot_id)
                .join(RiotAccount, DailyStoreSchedule.riot_account_id == RiotAccount.id)
                .where(DailyStoreSchedule.schedule_time == current_time)
            )
            schedules_to_run = result.all()

        for schedule, riot_id in schedules_to_run:
            try:
                channel = self.bot.get_channel(schedule.channel_id)
                if channel:
                    # メンションするユーザーを取得
                    user = self.bot.get_user(schedule.discord_user_id) or await self.bot.fetch_user(schedule.discord_user_id)
                    user_mention = user.mention if user else f"<@{schedule.discord_user_id}>"
                    mention = f"{user_mention} ({riot_id})"
                    
                    print(f"Running schedule for user {schedule.discord_user_id} in channel {schedule.channel_id}")
                    await self._send_daily_store_image(
                        riot_account_id=schedule.riot_account_id,
                        channel=channel,
                        mention=mention
                    )
            except Exception as e:
                print(f"Failed to run schedule {schedule.id}: {e}")

    @daily_store_task.before_loop
    async def before_daily_store_task(self):
        await self.bot.wait_until_ready()
        print("Starting daily store task loop...")


    @commands.Cog.listener()
    async def on_ready(self):
        await asyncio.gather(
            self.build_caches(),
            self.fetch_client_version()
        )

    async def build_caches(self):
        print("Building efficient skin caches from Valorant-API...")
        try:
            tiers = {}
            async with self.bot.http_session.get("https://valorant-api.com/v1/contenttiers?language=ja-JP") as r:
                r.raise_for_status()
                for tier in (await r.json())['data']:
                    hex_color = tier['highlightColor'].lstrip('#')[:6]
                    if hex_color:
                        tiers[tier['uuid']] = {
                            "name": tier['devName'], 
                            "color": discord.Color(int(hex_color, 16))
                        }

            async with self.bot.http_session.get("https://valorant-api.com/v1/weapons/skins") as r_skins:
                r_skins.raise_for_status()
                all_skins_data = (await r_skins.json())['data']
                
                async with self.bot.http_session.get("https://valorant-api.com/v1/weapons/skins?language=ja-JP") as r_skins_ja:
                    r_skins_ja.raise_for_status()
                    all_skins_ja_data = (await r_skins_ja.json())['data']
                    ja_names = {skin['uuid']: skin['displayName'] for skin in all_skins_ja_data}

                    for skin in all_skins_data:
                        tier_uuid = skin.get('contentTierUuid')
                        tier_info = tiers.get(tier_uuid, {})

                        self.skin_cache[skin['uuid']] = {
                            "name_ja": ja_names.get(skin['uuid'], skin['displayName']),
                            "name_en": skin['displayName'],
                            "rarity_name": tier_info.get("name", "Select"),
                            "color": tier_info.get("color", discord.Color.default()),
                            "icon": skin['displayIcon']
                        }
                        for level in skin['levels']:
                            self.level_to_skin_map[level['uuid']] = skin['uuid']
            
            print(f"Successfully built caches for {len(self.skin_cache)} skins and {len(self.level_to_skin_map)} levels.")
        except Exception as e:
            print(f"Failed to build caches: {e}")

    async def fetch_client_version(self):
        print("Fetching latest client version from Valorant-API...")
        try:
            async with self.bot.http_session.get("https://valorant-api.com/v1/version") as resp:
                resp.raise_for_status()
                data = await resp.json()
                self.client_version = data['data']['riotClientVersion']
                print(f"Client version fetched: {self.client_version}")
        except Exception as e:
            print(f"Failed to fetch client version: {e}")

    @account.command(name="link", description="Valorantアカウントを連携します。")
    async def link(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        state_token = str(uuid.uuid4())
        expiry_time = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=10)

        async with async_session() as session:
            async with session.begin():
                new_state = State(
                    state_token=state_token,
                    user_id=interaction.user.id,
                    expiry=expiry_time
                )
                session.add(new_state)

        auth_url = f"https://{self.your_domain}/auth?state={state_token}&client_id={self.bot.user.id}"

        ephemeral_embed = discord.Embed(
            title="アカウント連携を開始します",
            description=f"個別に送信されたDMの指示に従って、アカウント連携を完了してください。\nDMが届かない場合は、サーバーのプライバシー設定を確認してください。",
            color=discord.Color.blue()
        )
        await interaction.followup.send(embed=ephemeral_embed, ephemeral=True)

        dm_embed = discord.Embed(
            title="Valorantアカウント連携ガイド",
            description=(
                "**ステップ1: 拡張機能のインストール**\n"
                "連携には専用のChrome拡張機能が必要です。[こちら](https://github.com/SpikeBot-v2/SpikeBot-Extension/)からインストールしてください。\n\n"
                "**ステップ2: 認証ページへアクセス**\n"
                f"下のボタンまたは[こちらのリンク]({auth_url})をクリックしてRiot Gamesにログインしてください。\n"
                "ログインが完了すると、自動的に認証情報が安全にボットへ送信されます。\n\n"
                "**補足:**\n"
                "・複数のアカウントを連携できます。\n"
                "・アカウント名は連携時にRiot IDが自動で設定されます。\n"
                "・アカウント名の変更は `/account rename` で行えます。"
            ),
            color=discord.Color.green()
        )
        
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="認証ページへ移動", url=auth_url, style=discord.ButtonStyle.link))

        try:
            await interaction.user.send(embed=dm_embed, view=view)
        except discord.Forbidden:
            error_embed = discord.Embed(
                title="エラー: DMを送信できません",
                description="あなたへのDMが無効になっているため、連携リンクを送信できませんでした。\nサーバーのプライバシー設定で「サーバーにいるメンバーからのダイレクトメッセージを許可する」を有効にしてください。",
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=error_embed, ephemeral=True)

    @account.command(name="unlink", description="連携しているValorantアカウントを解除します。")
    async def unlink(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        async with async_session() as session:
            result = await session.execute(
                select(RiotAccount).where(RiotAccount.discord_user_id == interaction.user.id)
            )
            accounts = result.scalars().all()

        if not accounts:
            await interaction.followup.send("連携されているアカウントはありません。", ephemeral=True)
            return

        if len(accounts) == 1:
            account_to_delete = accounts[0]
            async with async_session() as session:
                async with session.begin():
                    # 関連するスケジュールも削除
                    await session.execute(delete(DailyStoreSchedule).where(DailyStoreSchedule.riot_account_id == account_to_delete.id))
                    await session.delete(account_to_delete)
            await interaction.followup.send(f"アカウント「{account_to_delete.account_name}」の連携を解除しました。", ephemeral=True)
            return
        
        async def callback(i: discord.Interaction, account_id: int):
            async with async_session() as session:
                async with session.begin():
                    result = await session.execute(
                        select(RiotAccount).where(
                            RiotAccount.id == account_id,
                            RiotAccount.discord_user_id == i.user.id
                        )
                    )
                    account = result.scalar_one_or_none()

                    if account:
                        account_name = account.account_name
                        # 関連するスケジュールも削除
                        await session.execute(delete(DailyStoreSchedule).where(DailyStoreSchedule.riot_account_id == account_id))
                        await session.delete(account)
                        await i.response.send_message(f"アカウント「{account_name}」の連携を解除しました。", ephemeral=True)
                    else:
                        await i.response.send_message("エラーが発生しました。対象のアカウントが見つからないか、権限がありません。", ephemeral=True)

        view = AccountSelectView(accounts, callback, placeholder="連携を解除するアカウントを選択")
        await interaction.followup.send("連携を解除するアカウントを選択してください。", view=view, ephemeral=True)

    @account.command(name="rename", description="連携済みアカウントのニックネームをRiot IDで指定して変更します。")
    @app_commands.describe(riot_id="ニックネームを変更したいアカウントのRiot ID (例: Steel#KR1)", new_name="新しいニックネーム")
    async def rename(self, interaction: discord.Interaction, riot_id: str, new_name: str):
        await interaction.response.defer(ephemeral=True)
        
        new_name = new_name.strip()
        if not new_name:
            await interaction.followup.send("新しいニックネームは空にできません。", ephemeral=True)
            return
        if len(new_name) > 50:
            await interaction.followup.send("新しいニックネームは50文字以内で入力してください。", ephemeral=True)
            return

        async with async_session() as session:
            async with session.begin():
                # 新しい名前が既に使われていないかチェック
                result = await session.execute(
                    select(RiotAccount).where(
                        RiotAccount.discord_user_id == interaction.user.id,
                        RiotAccount.account_name == new_name
                    )
                )
                if result.scalar_one_or_none():
                    await interaction.followup.send(f"エラー: 「{new_name}」という名前は既に使用されています。", ephemeral=True)
                    return

                # Riot IDでアカウントを検索して更新
                stmt = sqlalchemy_update(RiotAccount).where(
                    RiotAccount.discord_user_id == interaction.user.id,
                    RiotAccount.riot_id == riot_id
                ).values(account_name=new_name)
                
                result = await session.execute(stmt)

                if result.rowcount == 0:
                    await interaction.followup.send(f"エラー: Riot ID「{riot_id}」のアカウントが見つからないか、あなたのアカウントではありません。", ephemeral=True)
                else:
                    await interaction.followup.send(f"Riot ID「{riot_id}」のアカウントのニックネームを「{new_name}」に変更しました。", ephemeral=True)

    async def _execute_valorant_command(self, interaction: discord.Interaction, command_logic):
        """Valorant関連コマンドの共通処理（アカウント選択など）"""
        async with async_session() as session:
            result = await session.execute(
                select(RiotAccount).where(RiotAccount.discord_user_id == interaction.user.id)
            )
            accounts = result.scalars().all()

        if not accounts:
            embed = discord.Embed(title="アカウント未連携", description="`/account link` コマンドで先にアカウントを連携してください。", color=discord.Color.orange())
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        if len(accounts) == 1:
            await command_logic(interaction, accounts[0].id)
        else:
            async def callback(i: discord.Interaction, account_id: int):
                # コールバックからのインタラクションは ephemeral である必要がある場合が多い
                # is_followup を True にして、応答を適切に処理させる
                await command_logic(i, account_id, is_followup=True)

            view = AccountSelectView(accounts, callback, placeholder="情報を表示するアカウントを選択")
            await interaction.followup.send("情報を表示するアカウントを選択してください。", view=view, ephemeral=True)

    @store.command(name="daily", description="日替わりオファーを表示します。")
    async def store_daily(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self._execute_valorant_command(interaction, self._daily_logic)

    @store.command(name="bundle", description="現在のおすすめバンドルを表示します。")
    async def store_bundle(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self._execute_valorant_command(interaction, self._bundle_logic)

    async def _update_or_create_schedule(self, user_id: int, guild_id: int, channel_id: int, account_id: int, schedule_time: datetime.time, time_str: str) -> str:
        """Helper to update or create a schedule. Returns a confirmation message."""
        async with async_session() as session:
            async with session.begin():
                result = await session.execute(
                    select(DailyStoreSchedule).where(
                        DailyStoreSchedule.discord_user_id == user_id,
                        DailyStoreSchedule.guild_id == guild_id,
                        DailyStoreSchedule.channel_id == channel_id
                    )
                )
                existing_schedule = result.scalar_one_or_none()

                channel_mention = f"<#{channel_id}>"

                if existing_schedule:
                    existing_schedule.schedule_time = schedule_time
                    existing_schedule.riot_account_id = account_id
                    message = f"{channel_mention} の自動投稿スケジュールを、毎日 **{time_str}** に更新しました。"
                else:
                    new_schedule = DailyStoreSchedule(
                        discord_user_id=user_id,
                        riot_account_id=account_id,
                        guild_id=guild_id,
                        channel_id=channel_id,
                        schedule_time=schedule_time
                    )
                    session.add(new_schedule)
                    message = f"{channel_mention} に、毎日日本時間 **{time_str}** にデイリーストアを自動投稿するよう設定しました。"
            return message

    @schedule.command(name="add", description="このチャンネルにデイリーストアの自動投稿を予約します。")
    @app_commands.describe(time="投稿する時刻 (HH:MM形式, 24時間表記, 日本時間)")
    async def schedule_add(self, interaction: discord.Interaction, time: str):
        await interaction.response.defer(ephemeral=True)

        match = re.match(r"^([01]\d|2[0-3]):([0-5]\d)$", time)
        if not match:
            await interaction.followup.send("時刻は `HH:MM` 形式（例: `09:00`）で入力してください。", ephemeral=True)
            return
        
        schedule_time = datetime.time(hour=int(match.group(1)), minute=int(match.group(2)))

        async def account_select_callback(i: discord.Interaction, account_id: int):
            await i.response.defer(ephemeral=True)
            message = await self._update_or_create_schedule(
                user_id=i.user.id,
                guild_id=i.guild.id,
                channel_id=i.channel.id,
                account_id=account_id,
                schedule_time=schedule_time,
                time_str=time
            )
            await i.followup.send(message, ephemeral=True)

        async with async_session() as session:
            result = await session.execute(select(RiotAccount).where(RiotAccount.discord_user_id == interaction.user.id))
            accounts = result.scalars().all()

        if not accounts:
            await interaction.followup.send("連携されているアカウントがありません。`/account link`で先に連携してください。", ephemeral=True)
            return
        
        if len(accounts) == 1:
            message = await self._update_or_create_schedule(
                user_id=interaction.user.id,
                guild_id=interaction.guild.id,
                channel_id=interaction.channel.id,
                account_id=accounts[0].id,
                schedule_time=schedule_time,
                time_str=time
            )
            await interaction.followup.send(message, ephemeral=True)
        else:
            view = AccountSelectView(accounts, account_select_callback, placeholder="自動投稿に使用するアカウントを選択")
            await interaction.followup.send("自動投稿に使用するアカウントを選択してください。", view=view, ephemeral=True)

    @schedule.command(name="list", description="このサーバーに設定されているあなたのスケジュール一覧を表示します。")
    async def schedule_list(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        async with async_session() as session:
            result = await session.execute(
                select(DailyStoreSchedule, RiotAccount.account_name)
                .join(RiotAccount, DailyStoreSchedule.riot_account_id == RiotAccount.id)
                .where(DailyStoreSchedule.discord_user_id == interaction.user.id, DailyStoreSchedule.guild_id == interaction.guild.id)
            )
            schedules = result.all()

        if not schedules:
            await interaction.followup.send("このサーバーにはスケジュールが設定されていません。", ephemeral=True)
            return

        embed = discord.Embed(title=f"{interaction.guild.name}の自動投稿スケジュール", color=discord.Color.blue())
        description = ""
        for schedule, account_name in schedules:
            channel = self.bot.get_channel(schedule.channel_id)
            channel_mention = channel.mention if channel else f"`不明なチャンネル({schedule.channel_id})`"
            time_str = schedule.schedule_time.strftime('%H:%M')
            description += f"・ 毎日 **{time_str}** (日本時間) に {channel_mention} へ投稿 (使用アカウント: `{account_name}`)\n"
        
        embed.description = description
        await interaction.followup.send(embed=embed, ephemeral=True)

    @schedule.command(name="remove", description="自動投稿スケジュールを対話形式で削除します。")
    async def schedule_remove(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        async with async_session() as session:
            # このサーバーでユーザーが設定したスケジュールをすべて取得
            result = await session.execute(
                select(DailyStoreSchedule, RiotAccount.account_name)
                .join(RiotAccount, DailyStoreSchedule.riot_account_id == RiotAccount.id)
                .where(
                    DailyStoreSchedule.discord_user_id == interaction.user.id,
                    DailyStoreSchedule.guild_id == interaction.guild.id
                )
            )
            schedules = result.all()

        if not schedules:
            await interaction.followup.send("このサーバーには削除できるスケジュールが設定されていません。", ephemeral=True)
            return

        # ドロップダウンメニューの選択肢を作成
        options = []
        for schedule, account_name in schedules:
            channel = self.bot.get_channel(schedule.channel_id)
            channel_name = f"#{channel.name}" if channel else f"不明なチャンネル({schedule.channel_id})"
            time_str = schedule.schedule_time.strftime('%H:%M')
            label = f"毎日 {time_str} | {account_name} -> {channel_name}"
            options.append(discord.SelectOption(label=label, value=str(schedule.id)))

        # 選択メニューを持つViewを定義
        class ScheduleRemoveView(discord.ui.View):
            def __init__(self, options: list[discord.SelectOption]):
                super().__init__(timeout=180)
                self.select_menu = discord.ui.Select(placeholder="削除するスケジュールを選択してください", options=options)
                self.select_menu.callback = self.on_select
                self.add_item(self.select_menu)

            async def on_select(self, i: discord.Interaction):
                schedule_id_to_delete = int(self.select_menu.values[0])
                
                async with async_session() as s:
                    async with s.begin():
                        # ユーザーが所有するスケジュールのみを削除対象とする
                        stmt = delete(DailyStoreSchedule).where(
                            DailyStoreSchedule.id == schedule_id_to_delete,
                            DailyStoreSchedule.discord_user_id == i.user.id
                        )
                        await s.execute(stmt)
                
                await i.response.send_message(f"スケジュールを削除しました。", ephemeral=True)
                
                # 選択後にViewを無効化
                for item in self.children:
                    item.disabled = True
                await i.edit_original_response(view=self)
                self.stop()

        view = ScheduleRemoveView(options)
        await interaction.followup.send("削除するスケジュールを以下から選択してください。", view=view, ephemeral=True)


    async def _bundle_logic(self, interaction: discord.Interaction, account_id: int, is_followup: bool = False):
        """おすすめバンドル表示のコアロジック"""
        # is_followupがTrueなら、元のインタラクションは既に処理済みなので新しい応答を開始
        if is_followup:
            await interaction.response.defer(ephemeral=True)
            send = interaction.followup.send
        else:
            send = interaction.followup.send

        await send("ストア情報を取得しています...", ephemeral=True)

        try:
            store_data = await self._get_storefront_with_reauth(account_id)
        except Exception as e:
            embed = discord.Embed(title="認証エラー", description=f"アカウント情報の更新に失敗しました。\n`{e}`\n`/account link`コマンドで再連携してください。", color=discord.Color.red())
            await send(embed=embed, ephemeral=True)
            return

        try:
            bundle_data = store_data['FeaturedBundle']['Bundle']
            bundle_price = list(bundle_data['TotalDiscountedCost'].values())[0]
            bundle_uuid = bundle_data['DataAssetID']
            
            async with self.bot.http_session.get(f"https://valorant-api.com/v1/bundles/{bundle_uuid}?language=ja-JP") as r:
                if r.ok:
                    bundle_api_data = (await r.json())['data']
                    bundle_name = bundle_api_data['displayName']
                    
                    embed_bundle = discord.Embed(title=f"✨ {bundle_name}", color=discord.Color.gold())
                    embed_bundle.set_author(name=f"{bundle_price} VP", icon_url="https://static.wikia.nocookie.net/valorant/images/9/9d/Valorant_Points.png")
                    embed_bundle.set_image(url=bundle_api_data['displayIcon'])
                    
                    # ephemeralではないメッセージとして送信
                    await interaction.channel.send(embed=embed_bundle)
                    
                    # 元の "ストア情報を取得しています..." メッセージを削除
                    await interaction.delete_original_response()

                else:
                    await send("バンドル情報の取得に失敗しました。", ephemeral=True)
        except Exception as e:
            print(f"Could not process featured bundle: {e}")
            await send("バンドル情報の処理中にエラーが発生しました。", ephemeral=True)


    async def _daily_logic(self, interaction: discord.Interaction, account_id: int, is_followup: bool = False):
        """日替わりオファー表示のコアロジック（インタラクション起点）"""
        # is_followupはアカウント選択メニューからのコールバックを示す
        if is_followup:
            # 新しいインタラクションなので、ephemeralでdeferする
            await interaction.response.defer(ephemeral=True)
        
        # ephemeralなフォローアップメッセージを送信
        await interaction.followup.send("ストア情報を取得しています...", ephemeral=True)

        async with async_session() as session:
            riot_account = await session.get(RiotAccount, account_id)
            if not riot_account:
                await interaction.followup.send("エラー: アカウントが見つかりませんでした。", ephemeral=True)
                return
            riot_id = riot_account.riot_id
        
        mention = f"{interaction.user.mention} ({riot_id})"

        # 最終的なストア画像はパブリックに投稿する
        await self._send_daily_store_image(
            riot_account_id=account_id,
            channel=interaction.channel,
            mention=mention,
            send_func=None, # channel.send を使用させる
            is_ephemeral=False, # 公開メッセージにする
            interaction=None # ephemeralなインタラクションを操作させない
        )

    async def _send_daily_store_image(self, riot_account_id: int, channel: discord.TextChannel, mention: str, send_func=None, is_ephemeral: bool = False, interaction: discord.Interaction = None):
        """日替わりオファーの画像を作成して送信する共通関数"""
        send = send_func or channel.send
        
        try:
            store_data = await self._get_storefront_with_reauth(riot_account_id)
        except Exception as e:
            embed = discord.Embed(title="認証エラー", description=f"アカウント情報の更新に失敗しました。\n`{e}`\n`/account link`コマンドで再連携してください。", color=discord.Color.red())
            await send(embed=embed, ephemeral=is_ephemeral)
            return

        temp_image_paths = []
        final_image_path = None
        try:
            if not os.path.exists("temp_images"): os.makedirs("temp_images")

            daily_offers = store_data['SkinsPanelLayout']['SingleItemStoreOffers']
            offers_for_image = []

            for offer in daily_offers:
                skin_level_uuid = offer['Rewards'][0]['ItemID']
                parent_skin_uuid = self.level_to_skin_map.get(skin_level_uuid)
                if not parent_skin_uuid: continue
                
                skin_info = self.skin_cache.get(parent_skin_uuid)
                if not skin_info: continue

                image_url = skin_info.get('icon')
                async with self.bot.http_session.get(f"https://valorant-api.com/v1/weapons/skinlevels/{skin_level_uuid}") as r_level:
                    if r_level.ok:
                        level_data = (await r_level.json())['data']
                        if level_data.get('displayIcon'):
                            image_url = level_data['displayIcon']
                
                if not image_url: continue

                temp_path = f"temp_images/{uuid.uuid4()}.png"
                async with self.bot.http_session.get(image_url) as r_img:
                    if r_img.ok:
                        async with aiofiles.open(temp_path, mode='wb') as f:
                            await f.write(await r_img.read())
                        temp_image_paths.append(temp_path)
                        
                        skin_price = list(offer['Cost'].values())[0]
                        
                        offers_for_image.append({
                            "name_ja": skin_info['name_ja'], "name_en": skin_info['name_en'],
                            "image_path": temp_path, "rarity_name": skin_info.get('rarity_name', 'Select'),
                            "price": skin_price
                        })
            
            vp_icon_path = "assets/vp_icon.png"
            final_image_path = await asyncio.to_thread(create_daily_store_image, offers_for_image, vp_icon_path)

            if final_image_path:
                # ephemeralな場合はfollowup.sendを使い、そうでない場合はchannel.sendを使う
                if is_ephemeral:
                    await send(
                        content=f"{mention} のデイリーストア",
                        file=discord.File(final_image_path),
                        ephemeral=True
                    )
                    # 元の "取得中..." メッセージを削除
                    if interaction:
                        await interaction.delete_original_response()
                else:
                    await channel.send(
                        content=f"{mention} のデイリーストア",
                        file=discord.File(final_image_path)
                    )
            else:
                await send("画像の生成に失敗しました。", ephemeral=is_ephemeral)

        except Exception as e:
            print(f"Store command failed during image processing: {e}")
            await send("ストア情報の処理中にエラーが発生しました。", ephemeral=is_ephemeral)
        
        finally:
            # 非同期ファイル操作でイベントループのブロッキングを回避
            for path in temp_image_paths:
                try:
                    await aiofiles.os.remove(path)
                except FileNotFoundError:
                    pass
            if final_image_path:
                try:
                    await aiofiles.os.remove(final_image_path)
                except FileNotFoundError:
                    pass

    async def _get_storefront_with_reauth(self, account_id: int):
        """指定されたアカウントIDでストア情報を取得し、必要であれば再認証を行う"""
        async with async_session() as session:
            account = await session.get(RiotAccount, account_id)
            if not account:
                raise ValueError("指定されたアカウントが見つかりません。")

        try:
            return await self._get_storefront(account)
        except Exception as e:
            print(f"Initial store fetch failed for account {account.id}: {e}. Re-authenticating...")
            try:
                decrypted_cookies = self.fernet.decrypt(account.encrypted_cookies.encode()).decode()
                api = RiotAPI(self.bot.http_session)
                new_access_token, new_entitlement_token = await api.get_tokens_from_cookies(decrypted_cookies)
                
                async with async_session() as session:
                    async with session.begin():
                        # セッション内で再度アカウントオブジェクトを取得して更新する
                        account_to_update = await session.get(RiotAccount, account_id)
                        if account_to_update:
                            account_to_update.auth_token = new_access_token
                            account_to_update.entitlement_token = new_entitlement_token
                            session.add(account_to_update)
                            # 更新後のアカウント情報を次の処理で使えるようにする
                            account = account_to_update

                print(f"Re-authentication successful for account {account.id}. Retrying store fetch...")
                return await self._get_storefront(account)
            except Exception as reauth_error:
                print(f"Re-authentication failed for account {account.id}: {reauth_error}")
                raise Exception("アカウント情報の更新に失敗しました。") from reauth_error

    async def _get_storefront(self, account: RiotAccount):
        """ユーザーのトークンを使ってストアフロントAPIを叩くヘルパー関数"""
        if not self.client_version:
            await self.fetch_client_version()
            if not self.client_version:
                raise Exception("Could not retrieve client version. Valorant-API might be down.")

        headers = {
            'Authorization': f'Bearer {account.auth_token}',
            'X-Riot-Entitlements-JWT': account.entitlement_token,
            'X-Riot-ClientVersion': self.client_version,
            'X-Riot-ClientPlatform': CLIENT_PLATFORM
        }
        
        url = f"https://pd.{account.shard}.a.pvp.net/store/v3/storefront/{account.puuid}"
        
        async with self.bot.http_session.post(url, headers=headers, json={}) as r:
            if r.status == 400:
                error_body = await r.json()
                if error_body.get("errorCode") == "BAD_CLAIMS":
                     raise Exception("Riot API returned BAD_CLAIMS. Re-authentication required.")
                print(f"Riot API returned 400 Bad Request with body: {error_body}")
            
            r.raise_for_status()
            return await r.json()

async def setup(bot: commands.Bot, your_domain: str, fernet: Fernet):
    await bot.add_cog(ValorantCommands(bot, your_domain, fernet))