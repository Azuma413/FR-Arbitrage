import requests

def get_shared_coins():
    url = "https://api.hyperliquid.xyz/info"
    
    # 1. Perp市場の銘柄リストを取得
    perp_response = requests.post(url, json={"type": "meta"}).json()
    perp_coins = {asset["name"] for asset in perp_response["universe"]}
    
    # 2. Spot市場の銘柄リストを取得
    spot_response = requests.post(url, json={"type": "spotMeta"}).json()
    
    # spotMetaには "tokens" (各トークン情報) と "universe" (取引ペア情報) が含まれる
    spot_tokens = spot_response["tokens"]
    spot_universe = spot_response["universe"]
    
    spot_coins = set()
    for pair in spot_universe:
        # pair["tokens"] は [Baseトークンインデックス, Quoteトークン(USDC)インデックス]
        # 例: HYPE/USDC なら [150, 0] という配列になっている
        base_token_idx = pair["tokens"][0]
        
        # インデックスを使って本当のティッカー名を取得
        base_token_name = spot_tokens[base_token_idx]["name"]
        spot_coins.add(base_token_name)
        
    # 3. 両方に存在する銘柄（積集合）を抽出
    shared_coins = perp_coins.intersection(spot_coins)
    
    print(f"Perp市場の銘柄数: {len(perp_coins)}")
    print(f"Spot市場の銘柄数: {len(spot_coins)}")
    print(f"\n✅ 両建て(Spot/Perp)可能な銘柄 ({len(shared_coins)}個):")
    print(sorted(list(shared_coins)))

if __name__ == "__main__":
    get_shared_coins()