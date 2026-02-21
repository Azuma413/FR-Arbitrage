"""Application settings — Pydantic-based configuration for Hyperliquid Yield Harvester.

設定値はすべてこのファイルまたは `.env` ファイルで管理されます。
各パラメータの概要はコメントとして日本語で記載しています。
"""

from __future__ import annotations

from typing import List, Optional
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict



class Settings(BaseSettings):
    """Global configuration for the Hyperliquid Yield Harvester.

    Values are loaded from a `.env` file in the project root.
    Any field can be overridden by setting the corresponding environment variable.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # --- Network & Auth (ネットワークと認証) --------------------------------
    environment: str = "MAINNET"  # "MAINNET" または "TESTNET" (Hyperliquidの接続先環境)
    private_key: str = ""  # 0x... から始まるEthereumプレーン秘密鍵 (取引や署名に使用)
    account_address: str = ""  # 秘密鍵に紐づくウォレットアドレス (API実行時に必要)

    # --- Strategy Parameters (戦略パラメータ) -------------------------------
    target_coins: List[str] = ['AZTEC', 'BERA', 'HYPE', 'MON', 'PUMP', 'PURR', 'STABLE', 'TRUMP']  # 監視対象とするコイン(ティッカー)のリスト
    blacklist_coins: List[str] = []  # 取引対象から除外するコインのリスト

    # Entry Filters (エントリー条件・フィルター)
    min_funding_rate_hourly: float = 0.0001 # 最小資金調達率
    max_entry_spread: float = 0.0005  # 最大スプレッド許容値 (理論上スプレッドが0.1%以内ならエントリー)
    min_daily_volume: float = 500_000.0  # 最小建玉(OI)・取引高 ($500K以上ある流動性の高い銘柄のみ)

    # Execution Parameters (注文実行パラメータ)
    slippage_tolerance: float = 0.0005  # 許容スリッページ (0.2%。この範囲の価格変動なら注文を通す)
    max_retry_attempts: int = 3  # 注文が失敗した際の最大再試行回数

    # Position Sizing (ポジションサイズ・資金管理)
    max_position_usdc: float = 1000.0  # 1銘柄あたりの最大取引サイズ(USDC換算)
    max_open_positions: int = 5  # 同時に持てるポジションの最大数
    leverage_buffer: float = 0.55  # 無期限先物の担保割合 (例: 55%を無期限先物の証拠金として割り当てる)

    # --- Dry-Run Mode (テスト運用モード) ------------------------------------
    dry_run: bool = True  # Trueだと実際の注文を行わずシミュレーション(ペーパートレード)を実行
    dry_run_initial_balance: float = 6666.7  # Dry-Run用の初期仮想残高(USDC)

    # --- Position Guardian (ポジション管理・監視) ---------------------------
    fr_ma_window_hours: float = 24.0  # この時間(時間単位)での資金調達率の移動平均がマイナスになったら損切り
    margin_usage_threshold: float = 0.80  # 証拠金使用率がこの閾値(例:80%)を超えたらポジションサイズを縮小して調整(リバランス)
    db_url: str = "sqlite+aiosqlite:///./yield_harvester.db"  # ポジション状態を保存するデータベース(SQLite)のパス
    log_level: str = "INFO"  # ログレベル (DEBUG, INFO, WARNING, ERROR)

    # --- WandB Logging (Weights & Biasesを用いたログ収集) -------------------
    wandb_enabled: bool = False  # TrueにするとWandBへ稼働状況やメトリクスを送信
    wandb_project: str = "fr-arbitrage"  # WandB上のプロジェクト名
    wandb_entity: str = ""  # WandBのユーザー名またはチーム名 (任意)
    wandb_api_key: str = ""  # WandBのAPIキー (設定されていれば自動で有効化されます)
    
    @model_validator(mode="after")
    def _enable_wandb_if_key_present(self) -> Settings:

        if self.wandb_api_key and not self.wandb_enabled:
            self.wandb_enabled = True
        return self

    # --- Scan / Guardian intervals (seconds) (定期処理の間隔) ---------------
    scan_interval_sec: int = 60  # 新規エントリー機会を探す(スキャンする)間隔(秒)
    guardian_interval_sec: int = 30  # 保有ポジションの状態を監視・損切りチェックする間隔(秒)

    # --- Safety (セーフティ機能) --------------------------------------------
    emergency_stop: bool = False  # Trueにすると新規取引を一時停止し、緊急停止状態になる
