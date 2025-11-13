# cogs/webhook_listener.py (新規作成)
import discord
from discord.ext import commands
import json
import hmac
import hashlib
import base64
import datetime
import time
from cryptography.fernet import Fernet
from sqlalchemy.future import select
from sqlalchemy import update as sqlalchemy_update, delete as sqlalchemy_delete

from database.database import async_session
from database.models import State, RiotAccount
from api.riot_api import RiotAPI

class WebhookListenerCog(commands.Cog):
    def __init__(self, bot: commands.Bot, fernet: Fernet, hmac_secret: str, channel_id: int):
        self.bot = bot
        self.fernet = fernet
        self.hmac_secret = hmac_secret
        self.listen_channel_id = channel_id
        print(f"Listening for webhooks in channel ID: {self.listen_channel_id}")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # --- ここからデバッグコード ---
        print("-" * 40)
        print(f"[DEBUG] on_message fired in channel: {message.channel.id} (Author: {message.author})")

        # 1. チャンネルIDが一致するかチェック
        channel_id_match = message.channel.id == self.listen_channel_id
        print(f"[DEBUG] Configured Channel ID: {self.listen_channel_id}")
        print(f"[DEBUG] Condition check: Channel ID match? -> {channel_id_match}")
        # 2. Webhookからの投稿かチェック
        is_webhook = message.webhook_id is not None
        print(f"[DEBUG] Condition check: Is it a webhook? -> {is_webhook}")
        # --- ここまでデバッグコード ---
        # 元のif文
        # チャンネルIDは参考情報として扱い、HMAC検証で真正性を担保する
        # Webhookでなくても、HMACが正しいものは処理する（真正性はHMACで担保）
        if not is_webhook:
            print("[DEBUG] Not a webhook sender. Proceeding only if HMAC is valid.")

        if not channel_id_match:
            print("[DEBUG] Channel ID mismatch. Will attempt HMAC verification fallback.")
        else:
            print("[DEBUG] Channel ID match. Proceeding with processing.")

        print("[DEBUG] Starting to process incoming message...")

        try:
            # --- 受信データ取得ロジックを強化（添付ファイル優先 + 旧Embed方式も互換） ---
            payload_str = None
            hmac_signature = None

            # 1) 添付ファイル（payload.json）を最優先で取得
            if message.attachments:
                print(f"[DEBUG] Found {len(message.attachments)} attachments")
                for att in message.attachments:
                    name = (att.filename or "").lower()
                    ctype = (att.content_type or "").lower()
                    print(f"[DEBUG] Checking attachment: {name}, content_type: {ctype}")
                    if name.endswith(".json") or "application/json" in ctype:
                        content_bytes = await att.read()
                        payload_str = content_bytes.decode("utf-8", errors="replace")
                        print(f"[DEBUG] Loaded payload from attachment: {len(payload_str)} chars")
                        break

            # 2) 署名はEmbedの hmac_signature から取得（ファイル方式/旧方式 共通）
            if message.embeds:
                print(f"[DEBUG] Found {len(message.embeds)} embeds")
                try:
                    emb = message.embeds[0]
                    for field in emb.fields:
                        if field.name == "hmac_signature":
                            hmac_signature = (field.value or "").strip('`')
                            print(f"[DEBUG] Found HMAC signature: {hmac_signature[:10]}...")
                except Exception as e:
                    print(f"[DEBUG] Error reading embed fields: {e}")

            # 3) 互換: 旧方式（data_part_* フィールド分割）から復元
            if payload_str is None and message.embeds:
                try:
                    emb = message.embeds[0]
                    parts = []
                    for field in emb.fields:
                        if field.name.startswith("data_part_"):
                            parts.append((field.value or "").strip('`'))
                    if parts:
                        payload_str = "".join(parts)
                        print(f"[DEBUG] Loaded payload from old embed format: {len(payload_str)} chars")
                except Exception as e:
                    print(f"[DEBUG] Error reading old embed format: {e}")

            if not payload_str or not hmac_signature:
                print(f"[DEBUG] Missing payload or signature. payload_present={bool(payload_str)}, sig_present={bool(hmac_signature)}")
                return

            # --- 署名検証 ---
            digest = hmac.new(self.hmac_secret.encode(), payload_str.encode(), hashlib.sha256).digest()
            expected_signature = base64.b64encode(digest).decode()

            if not hmac.compare_digest(expected_signature, hmac_signature):
                print(f"[DEBUG] Invalid HMAC signature received in channel {message.channel.id}")
                return

            print("[DEBUG] HMAC signature verified successfully")

            # --- データ処理 ---
            data = json.loads(payload_str)
            state_token = data.get('state_token')
            cookies_str = data.get('cookies_str')
            access_token_from_payload = data.get('access_token')
            flow = data.get('flow')
            print(f"[DEBUG] Parsed payload data: state_token={state_token[:10] if state_token else None}..., has_cookies={bool(cookies_str)}, has_access_token={bool(access_token_from_payload)}, flow={flow}")

            # (ここから下の処理は、元のwebhook_handler.pyとほぼ同じ)
            async with async_session() as session:
                async with session.begin():
                    result = await session.execute(select(State).where(State.state_token == state_token))
                    state_obj = result.scalar_one_or_none()

                    if not state_obj or state_obj.expiry < datetime.datetime.now(datetime.timezone.utc):
                        print(f"[DEBUG] State token invalid or expired: exists={bool(state_obj)}, expired={state_obj.expiry < datetime.datetime.now(datetime.timezone.utc) if state_obj else 'N/A'}")
                        if state_obj: await session.execute(sqlalchemy_delete(State).where(State.state_token == state_token))
                        return

                    user_id = state_obj.user_id
                    print(f"[DEBUG] Found valid state for user {user_id}")
                    await session.execute(sqlalchemy_delete(State).where(State.state_token == state_token))

                try:
                    api = RiotAPI(self.bot.http_session)
                    print("[DEBUG] Starting Riot API authentication...")

                    if cookies_str:
                        # 従来フロー: Cookieからトークンを取得
                        print("[DEBUG] Using cookies flow")
                        access_token, entitlement_token = await api.get_tokens_from_cookies(cookies_str)
                    elif access_token_from_payload:
                        # 新規ログイン直後のフロー: access_tokenを優先利用
                        print("[DEBUG] Using access_token flow")
                        access_token = access_token_from_payload
                        entitlement_token = await api.get_entitlements_from_access_token(access_token)
                    else:
                        print("[DEBUG] Neither cookies nor access token present in payload.")
                        return

                    print("[DEBUG] Got tokens, fetching user info...")
                    puuid, riot_id = await api.get_user_info(access_token)
                    print(f"[DEBUG] Got user info: puuid={puuid[:10]}..., riot_id={riot_id}")
                except Exception as e:
                    print(f"[DEBUG] Riot API authentication failed for user {user_id}: {e}")
                    user = await self.bot.fetch_user(user_id)
                    await user.send("Valorantアカウントの認証に失敗しました。時間をおいて`/link`からやり直してください。")
                    return

                if cookies_str:
                    encrypted_cookies = self.fernet.encrypt(cookies_str.encode()).decode()
                else:
                    # Cookieが未取得の場合でもDB制約を満たすためのプレースホルダを保存（将来再連携を促す）
                    placeholder = f"ACCESS_TOKEN_ONLY::{user_id}::{int(time.time())}"
                    encrypted_cookies = self.fernet.encrypt(placeholder.encode()).decode()

                async with session.begin():
                    # 同じPUUIDを持つアカウントが既に連携されているか確認
                    result = await session.execute(
                        select(RiotAccount).where(
                            RiotAccount.discord_user_id == user_id,
                            RiotAccount.puuid == puuid
                        )
                    )
                    existing_account = result.scalar_one_or_none()
                    print(f"[DEBUG] Existing account check: {bool(existing_account)}")

                    if existing_account:
                        # 存在する場合、情報を更新
                        stmt = (
                            sqlalchemy_update(RiotAccount)
                            .where(RiotAccount.id == existing_account.id)
                            .values(
                                encrypted_cookies=encrypted_cookies,
                                auth_token=access_token,
                                entitlement_token=entitlement_token,
                                riot_id=riot_id # Riot IDも更新
                            )
                        )
                        await session.execute(stmt)
                        account_name = existing_account.account_name
                        print(f"[DEBUG] Updated existing account: {account_name}")
                    else:
                        # 存在しない場合、新規作成
                        # account_nameの重複をチェック
                        result = await session.execute(
                            select(RiotAccount).where(
                                RiotAccount.discord_user_id == user_id,
                                RiotAccount.account_name == riot_id
                            )
                        )
                        if result.scalar_one_or_none():
                            # もしRiot IDが既に別名として使われていたら、末尾にランダムな数字を追加
                            account_name = f"{riot_id}_{int(time.time()) % 1000}"
                        else:
                            account_name = riot_id

                        new_account = RiotAccount(
                            discord_user_id=user_id,
                            account_name=account_name,
                            riot_id=riot_id,
                            encrypted_cookies=encrypted_cookies,
                            auth_token=access_token,
                            entitlement_token=entitlement_token,
                            puuid=puuid
                        )
                        session.add(new_account)
                        print(f"[DEBUG] Created new account: {account_name}")

            user = await self.bot.fetch_user(user_id)
            embed = discord.Embed(
                title="✅ アカウント連携 成功",
                description=f"Valorantアカウント **{riot_id}** の連携が正常に完了しました！\n`/store`コマンドでデイリーストアを確認できます。",
                color=discord.Color.green(),
                timestamp=datetime.datetime.now(datetime.timezone.utc)
            )

            await user.send(embed=embed)
            print(f"[DEBUG] Successfully processed authentication for user {user_id} with Riot ID {riot_id}")

        except Exception as e:
            print(f"[DEBUG] An error occurred in on_message webhook processing: {e}")
            import traceback
            traceback.print_exc()

async def setup(bot: commands.Bot, fernet: Fernet, hmac_secret: str, channel_id: int):
    await bot.add_cog(WebhookListenerCog(bot, fernet, hmac_secret, channel_id))