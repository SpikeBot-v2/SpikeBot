# image_generator.py
import os
import uuid
from PIL import Image, ImageDraw, ImageFont, ImageFilter

# --- 設定 ---
CARD_SIZE = (550, 300)  # 生成するカード1枚のサイズ
FONT_PATH = "assets/BebasNeue-Regular.ttf"
JA_FONT_PATH = "assets/NotoSansJP-Medium.ttf"
FONT_SIZE = 12
JA_FONT_SIZE = 24
PRICE_FONT_SIZE = 24
VP_ICON_SIZE = (30, 30)
TEXT_COLOR = (255, 255, 255)
TEXT_STROKE_COLOR = (0, 0, 0)
OUTPUT_DIR = "temp_images"

# レアリティの色名と背景ファイルのマッピング
RARITY_BACKGROUNDS = {
    "Select": "assets/blue.png",
    "Deluxe": "assets/green.png",
    "Premium": "assets/red.png",
    "Exclusive": "assets/yellow.png",
    "Ultra": "assets/orange.png",
}

def create_daily_store_image(offers_data: list, vp_icon_path: str) -> str:
    if not os.path.exists(OUTPUT_DIR):
        os.makedirs(OUTPUT_DIR)

    # ★★★ 変更点: VPアイコンを最初に一度だけ読み込む ★★★
    try:
        vp_icon = Image.open(vp_icon_path).convert("RGBA")
        vp_icon.thumbnail(VP_ICON_SIZE, Image.Resampling.LANCZOS)
    except (IOError, TypeError):
        vp_icon = None # 読み込み失敗してもエラーにしない

    card_paths = []
    for offer in offers_data:
        try:
            # --- 1. カード1枚を生成 ---
            # レアリティに合った背景を読み込む
            bg_path = RARITY_BACKGROUNDS.get(offer['rarity_name'], "assets/blue.png")
            background = Image.open(bg_path).convert("RGBA").resize(CARD_SIZE)
            
            # 武器画像を読み込む
            weapon_image = Image.open(offer['image_path']).convert("RGBA")

            # 武器画像をカードサイズに合わせる
            weapon_image.thumbnail((CARD_SIZE[0] * 0.85, CARD_SIZE[1] * 0.6), Image.Resampling.LANCZOS)
            
            # ★★★ ここからが修正点 ★★★
            
            # 1. 元の武器画像より一回り大きい、完全に透明なキャンバスを作成
            #    余白の大きさ (padding) はぼかし半径より大きくする
            padding = 20 
            expanded_size = (weapon_image.width + padding * 2, weapon_image.height + padding * 2)
            expanded_canvas = Image.new("RGBA", expanded_size, (0, 0, 0, 0))

            # 2. 透明なキャンバスの中央に、元の武器画像を貼り付け
            paste_pos = (padding, padding)
            expanded_canvas.paste(weapon_image, paste_pos)

            blurred_expanded = expanded_canvas.filter(ImageFilter.GaussianBlur(radius=10))
            
            opacity = 0.4
            alpha = blurred_expanded.getchannel('A')
            
            alpha = alpha.point(lambda p: p * opacity)
            
            blurred_expanded.putalpha(alpha)

            blurred_weapon = blurred_expanded

            sharp_weapon = weapon_image

            # --- 2. 画像を合成 ---
            # (1) 背景の上に、半透明になったぼかし武器画像を中央に配置
            pos_blur = ((CARD_SIZE[0] - blurred_weapon.width) // 2, (CARD_SIZE[1] - blurred_weapon.height) // 2)
            background.paste(blurred_weapon, pos_blur, blurred_weapon)
            
            # (2) その上に、鮮明な武器画像を中央に配置
            pos_sharp = ((CARD_SIZE[0] - sharp_weapon.width) // 2, (CARD_SIZE[1] - sharp_weapon.height) // 2)
            background.paste(sharp_weapon, pos_sharp, sharp_weapon)

            # --- 3. テキストと価格アイコンを書き込む ---
            draw = ImageDraw.Draw(background)

            # 左下のテキスト (あなたのコードのまま)
            font_en = ImageFont.truetype(FONT_PATH, FONT_SIZE)
            text_en = offer['name_en']
            draw.text((20, CARD_SIZE[1] - FONT_SIZE - 45), text_en, font=font_en, fill=TEXT_COLOR)
            
            font_ja = ImageFont.truetype(JA_FONT_PATH, JA_FONT_SIZE)
            text_ja = offer['name_ja']
            draw.text((20, CARD_SIZE[1] - JA_FONT_SIZE - 25), text_ja, font=font_ja, fill=TEXT_COLOR)

            # ★★★ 新規追加: 右上の価格とアイコンを描画 ★★★
            if vp_icon and 'price' in offer:
                price_text = str(offer['price'])
                price_font = ImageFont.truetype(FONT_PATH, PRICE_FONT_SIZE)
                
                padding_right = 20
                spacing = 8

                # テキストの幅を取得
                text_width = draw.textlength(price_text, font=price_font)
                
                # アイコンの貼り付け位置を計算 (右端から)
                icon_x = CARD_SIZE[0] - padding_right - vp_icon.width
                icon_y = padding_right
                
                # テキストの描画位置を計算 (アイコンの左隣)
                text_x = icon_x - spacing - text_width
                # アイコンとテキストが垂直方向に中央揃えになるようにY座標を調整
                text_y = icon_y + (vp_icon.height - PRICE_FONT_SIZE) / 2 - 2 # 微調整値

                # 描画と貼り付け
                draw.text((text_x, text_y), price_text, font=price_font, fill=TEXT_COLOR)
                background.paste(vp_icon, (icon_x, icon_y), vp_icon)


            # 生成したカードを一時保存
            card_path = os.path.join(OUTPUT_DIR, f"{uuid.uuid4()}.png")
            background.save(card_path)
            card_paths.append(card_path)

        except Exception as e:
            print(f"カード画像の生成に失敗: {e}")
            continue

    if not card_paths:
        return None

    # --- 4. 2x2のグリッドに合成 ---
    grid_size = (CARD_SIZE[0] * 2, CARD_SIZE[1] * 2)
    grid_image = Image.new("RGBA", grid_size)
    
    positions = [(0, 0), (CARD_SIZE[0], 0), (0, CARD_SIZE[1]), (CARD_SIZE[0], CARD_SIZE[1])]
    
    for i, path in enumerate(card_paths):
        if i < 4:
            card = Image.open(path)
            grid_image.paste(card, positions[i])
            card.close()

    final_image_path = os.path.join(OUTPUT_DIR, f"final_{uuid.uuid4()}.png")
    grid_image.save(final_image_path)
    grid_image.close()

    # --- 5. 一時ファイルを削除 ---
    for path in card_paths:
        try:
            os.remove(path)
        except OSError as e:
            print(f"一時ファイルの削除に失敗: {e}")

    return final_image_path