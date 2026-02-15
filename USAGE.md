## メイン
```bash
uv run fr-arbitrage
```


## Wandbログについて
### trade
- entry

新規ポジション構築が完了したことを示す

- symbol

取引が完了した通貨ペア

- entry_price

現物買いとPerp売りの加重平均エントリー価格

- size

取引した現物の数量

- notional_usdc

取引の想定元本

- exit

ポジション解消が完了したことを示す

- exit_type

エグジットの種類（通常はfull）

### wallet
- withdrawable

出金可能額

- margin_used

現在使用中の証拠金総額

- margin_usage_pct

証拠金使用率

- account_value

口座の総資産価格

### guardian
- trigger_exit_negative_fr

Funding Rateがマイナスになったため決裁したことを示す

- consecutive_negative_fr

マイナスFRが何回連続で観測されたか

- trigger_exit_backwardation

スプレッドがバックワーデーション（現物価格＜Perp価格）になり，利益確定のチャンスと判断して決裁したことを示す

- spread

その時のスプレッド値