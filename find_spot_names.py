import asyncio
from hyperliquid.info import Info
from hyperliquid.utils import constants

async def main():
    info = Info(constants.MAINNET_API_URL, skip_ws=True)
    
    spot_meta = info.spot_meta()
    tokens = spot_meta.get("tokens", [])
    universe = spot_meta.get("universe", [])
    
    # Map token index to name
    token_map = {i: t["name"] for i, t in enumerate(tokens)}
    
    print(f"Found {len(universe)} spot assets.")
    
    targets = ["HYPE", "SOL", "ETH", "PURR", "BTC"]
    
    for idx, asset in enumerate(universe):
        asset_name = asset.get("name")
        base_token_idx = asset.get("tokens")[0]
        quote_token_idx = asset.get("tokens")[1]
        
        base_name = token_map.get(base_token_idx, f"Unknown({base_token_idx})")
        quote_name = token_map.get(quote_token_idx, f"Unknown({quote_token_idx})")
        
        display_name = f"{base_name}/{quote_name}"
        
        if True: # Print all
            print(f"Universe Index: {idx} | Name: {asset_name} | Pair: {display_name} | ID: {asset.get('index')}")

if __name__ == "__main__":
    asyncio.run(main())
