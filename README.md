# プロジェクト仕様書: FR-Arbitrage (High-Vol Edition)

## 1. プロジェクト概要

本システムは、暗号資産取引所（主対象: Bybit/Binance）において、現物（Spot）と無期限先物（Perpetual）のポジションを同量保有することでデルタニュートラル状態を維持し、高ボラティリティなアルトコインから発生する高額なFunding Rate（資金調達率）を自動で獲得するシステムである。

* **目標:** 月利 2.0% 〜 5.0%（年利換算 24% 〜 60%）の安定運用
* **コア戦略:** 金利差アービトラージ（Cash & Carry Trade）
* **主要な敵:** 執行スリッページ、APIレート制限、片側約定（Leg risk）、急激な相場変動によるロスカット

## 2. 技術スタック & 環境

* **言語:** Python 3.10+
* **主要ライブラリ:**
* `ccxt` (Async必須): 取引所API操作
* `asyncio`: 非同期処理
* `pandas`: データフレーム操作（計算用）
* `pydantic`: データモデル定義・バリデーション
* `loguru`: ロギング
* `python-dotenv`: 環境変数管理


* **DB/ストレージ:** `SQLite` (軽量な状態管理用) + `JSON` (設定ファイル)
* **環境:** Docker (推奨) または Systemd (Linux Deamon)

## 3. システムアーキテクチャ

システムは以下の4つの独立した非同期モジュールで構成される。

1. **MarketScanner (市場監視)**: 全ペアのFR、出来高、スプレッドを監視。
2. **OrderManager (注文執行)**: デルタニュートラルを維持したAtomicな売買執行。
3. **PositionGuardian (ポジション管理)**: 証拠金維持率の監視とリバランス。
4. **HealthMonitor (死活監視)**: エラーハンドリングと緊急停止。

---

## 4. 詳細ロジック仕様

### 4.1. MarketScanner (Entry Criteria)

以下の条件を全て満たす銘柄を「ターゲット」として選定する。

* **スキャン頻度:** 1分に1回
* **フィルタリング条件:**
1. **Quote Currency:** USDT (例: `XXX/USDT`)
2. **Funding Rate:** 直近のFunding Rate予測値が `0.03%` (8時間) 以上（年利換算 約32%以上）であること。
* *※高ボラティリティ狙いのため閾値を高めに設定*


3. **Liquidity (Volume):** 24時間出来高が `10,000,000 USDT` 以上であること（流動性枯渇リスク回避）。
4. **Spread:** 現物と先物の価格差（Premium）が `0.2%` 以上、かつ正の乖離（先物 > 現物）であること。
* *これにより、エントリー瞬間の含み損（スプレッド負け）を最小化する。*





### 4.2. OrderManager (Execution Logic)

**最重要コンポーネント。** 片側約定（Leg Risk）を防ぐため、以下の手順で執行する。

* **執行アルゴリズム:** "Concurrent Taker" (同時成行)
* 指値（Maker）は片側約定リスクが高いため、開発初期は成行（Taker）で同期させることを優先する。手数料負けは高FRで回収する設計。


* **プロセス:**
1. `min_trade_amount`（最小取引単位）と`step_size`（刻み値）をAPIから取得。
2. 投資予定額（例: 1000 USDT）から数量を算出。
3. **[Async Task]** 現物買い注文（Market Buy）を発行。
4. **[Async Task]** 先物売り注文（Market Short）を発行。
5. `asyncio.gather` で両方の約定を待機。
6. **Error Handling (Leg Recovery):**
* もし片方だけ約定した場合（例：現物は買えたが、先物がエラーで弾かれた）、即座に約定した方を反対売買（Market Sell/Cover）してポジションを解消する。**絶対にポジションを放置しない。**





### 4.3. PositionGuardian (Rebalancing & Exit)

高ボラティリティアルトコインは短時間で50%以上動くことがあるため、証拠金管理が生命線となる。

* **監視頻度:** 10秒に1回
* **リバランス・ロジック (Rebalancing):**
* 先物口座の証拠金維持率（Margin Level）を監視。
* 条件: `Margin Level < 20%` (あるいは取引所の危険水域) に達した場合。
* アクション:
1. 現物口座（Spot Wallet）から先物口座（Futures Wallet）へ USDT を自動振替（Transfer）。
2. 現物口座にUSDTが不足している場合、保有している現物ポジションの一部を売却し、同量の先物ショートを決済（ポジション縮小）して維持率を回復させる。




* **エグジット・ロジック (Exit Criteria):**
* **FR低下:** Funding Rateが `0.005%` 以下に低下、またはマイナス化した場合。
* **緊急停止:** スプレッドがマイナス（逆乖離） `1.0%` 以上に拡大した場合。



---

## 5. データモデル設計 (Pydantic / SQLite)

### 5.1. `TargetSymbol` (メモリ保持)

```python
class TargetSymbol(BaseModel):
    symbol: str          # e.g., "DOGE/USDT"
    funding_rate: float  # e.g., 0.0004 (0.04%)
    spot_price: float
    perp_price: float
    spread_pct: float    # (perp - spot) / spot
    volume_24h: float

```

### 5.2. `ActivePosition` (DB保存: `positions` テーブル)

```python
class ActivePosition(BaseModel):
    id: str              # UUID
    symbol: str
    entry_timestamp: int
    spot_qty: float      # 保有現物数量
    perp_qty: float      # 保有ショート数量
    entry_spread: float  # エントリー時の乖離率
    total_fees: float    # 手数料合計（USDT）
    status: str          # "OPEN", "CLOSING", "CLOSED"

```

---

## 6. エラーハンドリング & 安全装置 (Kill Switch)

本番稼働において最も重視すべき「守り」の仕様。

1. **API Rate Limit Management:**
* `ccxt`のレートリミッター機能を有効化 (`enableRateLimit=True`)。
* `429 Too Many Requests` が返ってきた場合、指数バックオフ（Exponential Backoff）で待機時間を増やして再試行。


2. **Max Open Positions:**
* 同時保有銘柄数は最大 `3` 銘柄に制限（資金分散とリスク管理のため）。


3. **Global Kill Switch:**
* プログラム起動時に `.env` ファイルまたは環境変数を読み込む。
* `EMERGENCY_STOP=True` が検知された場合、新規エントリーを停止し、全てのポジションを成行でクローズしてプログラムを終了するモードを実装する。



---

## 7. 開発・実装ステップ（Antigravityへの指示用）
以下の順番で実装。

**Phase 1: 接続テストとデータ取得**

> `ccxt`を使ってBybit（またはBinance）に接続し、USDT無期限先物の全銘柄から「Funding Rate」「24h Volume」「現在価格」を取得して、FRが高い順にランキング表示するPythonスクリプトを作成してください。クラス設計は`MarketScanner`として分離してください。

**Phase 2: 執行ロジックの実装（最重要）**

> `OrderManager`クラスを作成してください。`asyncio`を使って、指定した銘柄のSpot買いとPerp売りを同時に成行で執行するメソッド `execute_entry(symbol, amount_usdt)` を実装してください。片側約定エラー時のロールバック処理（即時決済）も含めてください。

**Phase 3: 監視とループ**

> これらを統合し、常駐して市場を監視し、条件に合致したらエントリー、証拠金維持率が低下したら警告ログを出すメインループを作成してください。