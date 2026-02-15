import asyncio
from hyperliquid.info import Info
from hyperliquid.utils import constants

async def main():
    info = Info(constants.MAINNET_API_URL, skip_ws=True)
    
    print("--- Spot Meta ---")
    spot_meta = info.spot_meta()
    universe = spot_meta.get("universe", [])
    for idx, asset in enumerate(universe):
        name = asset.get("name")
        print(f"Index: {idx}, Name: {name}, Tokens: {asset.get('tokens')}")
        if name in ["HYPE", "SOL", "ETH", "PURR/USDC", "HYPE/USDC"]:
            print(f"  -> FOUND INTERESTING ASSET: {name}")

    print("\n--- Perp Meta ---")
    meta = info.meta()
    perp_universe = meta.get("universe", [])
    for idx, asset in enumerate(perp_universe):
        name = asset.get("name")
        if name in ["HYPE", "SOL", "ETH", "PURR"]:
            print(f"Perp Index: {idx}, Name: {name}")

if __name__ == "__main__":
    asyncio.run(main())
