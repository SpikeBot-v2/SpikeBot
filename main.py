import discord
from discord import app_commands
from discord.ext import commands
import os
from dotenv import load_dotenv
import aiohttp
from cryptography.fernet import Fernet

from database.database import init_db
from cogs.valorant_commands import setup as setup_valorant_commands
# 新しいCogをインポート
from cogs.webhook_listener import setup as setup_webhook_listener

load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
YOUR_DOMAIN = os.getenv("YOUR_DOMAIN")
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")
HMAC_SECRET = os.getenv("HMAC_SECRET")
# 新しい環境変数を読み込む
WEBHOOK_CHANNEL_ID = int(os.getenv("WEBHOOK_CHANNEL_ID"))

intents = discord.Intents.default()
intents.message_content = True # on_messageのために必要
intents.messages = True      # on_messageのために必要

class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        self.http_session = None

    async def setup_hook(self):
        # ★★★ ここから変更 ★★★
        # 全てのリクエストに含める共通ヘッダーを定義
        common_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36"
        }
        # セッション作成時に共通ヘッダーを設定
        self.http_session = aiohttp.ClientSession(headers=common_headers)
        # ★★★ ここまで変更 ★★★
        
        await init_db()
        print("Database initialized.")

        fernet = Fernet(ENCRYPTION_KEY.encode())
        
        await setup_valorant_commands(self, YOUR_DOMAIN, fernet)
        print("Valorant commands loaded.")
        await setup_webhook_listener(self, fernet, HMAC_SECRET, WEBHOOK_CHANNEL_ID)
        print("Webhook listener loaded.")

        await self.tree.sync()
        print("Commands synced.")

    async def on_ready(self):
        print(f'Logged in as {self.user} (ID: {self.user.id})')
        print('------')

    async def close(self):
        if self.http_session:
            await self.http_session.close()
        await super().close()

bot = MyBot()

# (エラーハンドラーは変更なし)
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    # 正しいエラークラスを参照するように修正
    if isinstance(error, app_commands.CommandOnCooldown):
        await interaction.response.send_message(f"コマンドはクールダウン中です。{error.retry_after:.2f}秒後にもう一度お試しください。", ephemeral=True)
    elif isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("このコマンドを実行する権限がありません。", ephemeral=True)
    else:
        # ログには元のエラーも表示させるとデバッグしやすい
        original_error = getattr(error, 'original', error)
        print(f"Unhandled error in command '{interaction.command.name}': {original_error}")
        
        # 応答が完了しているか確認してから送信する、より安全な方法
        if interaction.response.is_done():
            await interaction.followup.send("コマンドの実行中に予期せぬエラーが発生しました。", ephemeral=True)
        else:
            await interaction.response.send_message("コマンドの実行中に予期せぬエラーが発生しました。", ephemeral=True)


if __name__ == "__main__":
    if not all([DISCORD_TOKEN, YOUR_DOMAIN, ENCRYPTION_KEY, HMAC_SECRET]):
        print("エラー: .envファイルに必要な設定が不足しています。")
    else:
        bot.run(DISCORD_TOKEN)