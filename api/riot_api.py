# api/riot_api.py
import aiohttp
import base64
import json

# Valorantのクライアント情報をBase64エンコードしたもの (固定値)
CLIENT_PLATFORM = base64.b64encode(
    b'{"platformType":"PC","platformOS":"Windows","platformOSVersion":"10.0.19042.1.256.64bit","platformChipset":"Unknown"}'
).decode()

class RiotAPI:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    async def get_tokens_from_cookies(self, cookies_str: str) -> tuple[str, str]:
        """Cookie文字列を使用して認証トークンとEntitlementトークンを取得する"""
        headers = {
            'Content-Type': 'application/json',
            'Cookie': cookies_str,
        }
        payload = {
            "client_id": "play-valorant-web-prod",
            "nonce": "1",
            "redirect_uri": "https://playvalorant.com/opt_in",
            "response_type": "token id_token",
            "scope": "account openid",
        }
        
        async with self.session.post('https://auth.riotgames.com/api/v1/authorization', json=payload, headers=headers) as r:
            r.raise_for_status()
            response_data = await r.json()
            
            # レスポンスのURIからアクセストークンを抽出
            uri = response_data['response']['parameters']['uri']
            access_token = uri.split('access_token=')[1].split('&')[0]

        # Entitlementトークンを取得
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json',
        }
        async with self.session.post('https://entitlements.auth.riotgames.com/api/token/v1', headers=headers, json={}) as r:
            r.raise_for_status()
            entitlements_token = (await r.json())['entitlements_token']

        return access_token, entitlements_token

    async def get_entitlements_from_access_token(self, access_token: str) -> str:
        """アクセストークンからEntitlementトークンのみを取得する"""
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json',
        }
        async with self.session.post('https://entitlements.auth.riotgames.com/api/token/v1', headers=headers, json={}) as r:
            r.raise_for_status()
            return (await r.json())['entitlements_token']

    async def get_user_info(self, access_token: str) -> tuple[str, str]:
        """アクセストークンを使用してPUUIDとRiot IDを取得する"""
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json',
        }
        async with self.session.get('https://auth.riotgames.com/userinfo', headers=headers) as r:
            r.raise_for_status()
            user_info = await r.json()
            
            puuid = user_info['sub']
            game_name = user_info.get('acct', {}).get('game_name', '')
            tag_line = user_info.get('acct', {}).get('tag_line', '')
            
            riot_id = f"{game_name}#{tag_line}"
            
            return puuid, riot_id