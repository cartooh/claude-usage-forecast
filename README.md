# claude-usage-forecast

Claude(Pro/Max)の週間使用量を Claude Code から取り出し、Google スプレッドシートに記録する。
複数の PC で動かすと、トークン数と API 換算額は全 PC の合計になる。上限に届きそうなら通知もできる(既定はオフ)。

```
各 PC:  Claude Code の描画 → hooks/statusline_hook.py → ~/.claude/usage-state.json
        cron(30分ごと) → bin/run.sh → scripts/usage_probe.py(/usage 画面)
                                      → usage_forecast.collector → Google Sheets
```

## 最初の準備(1回だけ)

記録先のスプレッドシートと、書き込みに使うサービスアカウントを用意する。
スプレッドシート自体はサービスアカウントでは新規作成できない(保存容量を持たないため)。
空のスプレッドシートを人が作って共有すれば、中のシートは `install.sh` が作る。

### 1. サービスアカウントとキーを作る

1. https://console.cloud.google.com/ でプロジェクトを作る(例: `claude-usage-forecast`)。
2. 「API とサービス」→「ライブラリ」で **Google Sheets API** を有効にする。Drive API は不要。
3. 「IAM と管理」→「サービス アカウント」→「サービス アカウントを作成」。名前は例えば `usage-writer`。
   ロールの付与は不要。
4. 作ったアカウントを開き、「キー」→「鍵を追加」→「新しい鍵を作成」→ **JSON**。ファイルが1つダウンロードされる。
5. このファイルが `--credentials` に渡すキー。リポジトリやチャットには置かない。

キーの中の `client_email` が、共有に使うメールアドレス:

```
python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["client_email"])' ~/service-account.json
# 例: usage-writer@my-project-123456.iam.gserviceaccount.com
```

組織のポリシーで「サービス アカウント キーの作成が無効」と出る場合は、個人の Google アカウントのプロジェクトで作る。

### 2. スプレッドシートを作って共有する

1. https://sheets.new を開くか、Google ドライブの「新規」→「Google スプレッドシート」で空のファイルを作る。
   名前は自由(例: `Claude週間使用量ペース表`)。
2. 「ファイル」→「設定」→「タイムゾーン」を **(GMT+09:00) 東京** にして保存する。
   「ペース表」の数式が NOW() を使うため。ペース表の「時差補正(h)」は 0 のままにする。
3. 右上の「共有」に、上の `client_email` を入れ、権限を **編集者** にする。
   「通知」のチェックは外してよい。「送信」(または「共有」)を押し、最後に「完了」を押す。
   入力しただけでダイアログを閉じると共有されない。
4. もう一度「共有」を開き、`client_email` が「編集者」で一覧に残っていることを確認する。

### 3. スプレッドシートの ID を取り出す

ID は URL の `/d/` と次の `/` の間の文字列:

```
https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcdEf/edit#gid=0
                                       └──────────────── これが ID ────────────────┘
```

同じ名前のファイルが複数あると取り違えやすい。ブラウザで開いているファイルの URL から取る。

### 4. 接続を確かめる

`install.sh` の途中で自動的に確認する。ID やキーを差し替えて確かめ直すときは、`install.sh` を一度実行した PC で:

```
SPREADSHEET_ID=<ID> GOOGLE_APPLICATION_CREDENTIALS=~/service-account.json \
  .venv/bin/python -m usage_forecast.collector --check-access
```

| 表示 | 意味と対処 |
|---|---|
| `OK: 「…」を開けました (タイムゾーン Asia/Tokyo)` | 準備完了 |
| `NG: HTTP 403 The caller does not have permission` | 共有されていない。手順 2-3 をやり直す。別のファイルを共有していないかも確認 |
| `NG: HTTP 403 … has not been used in project …` | Sheets API が無効。手順 1-2 |
| `NG: HTTP 404` | ID が違う。手順 3 |

タイムゾーンが `Asia/Tokyo` 以外と表示された場合は、手順 2-2 を行う。

## 新しい PC に入れる

前提: Linux または macOS、python3 3.9 以上、crontab、Claude Code。tmux があると Fable 枠と従量課金の欄も記録する。

1. サービスアカウントの JSON キーをその PC にコピーする(リポジトリには入れない)。
2. リポジトリを取得して `install.sh` を実行する。

```
git clone <このリポジトリ> ~/claude-usage-forecast
cd ~/claude-usage-forecast
./install.sh --spreadsheet-id <ID> --credentials ~/service-account.json --machine <この PC の名前>
```

`install.sh` が行うこと(再実行しても安全):

| 手順 | 内容 |
|---|---|
| 環境 | `.venv` を作り、依存を入れる |
| 設定 | `.env` を書く(600)。キーは `~/.config/claude-usage-forecast/` にコピーする |
| シート | 接続を確認し、無いシート(ペース表・ログ・推移グラフ・端末別集計)を作る |
| statusline | 未設定なら設定する。既存の statusline があれば包む(元の表示はそのまま) |
| cron | 30分ごとの `bin/run.sh` を1行追加する。他のジョブはそのまま残す |

`~/.claude/settings.json` と crontab は、変更前に `var/backups/` へ保存する。
`--machine` を省くとホスト名を使う。`--no-cron` / `--no-statusline` で個別に外せる。

`--spreadsheet-id` と `--credentials` の用意は、次の「最初の準備」を参照。
2台目以降の PC では、1台目と同じ ID と同じキーを使う。

## 外す

```
./uninstall.sh                 # cron の行を消し、statusline を元に戻す
./uninstall.sh --remove-data   # さらに「端末別集計」からこの PC の行を消す(ログの行は共有の履歴なので残す)
./uninstall.sh --purge         # さらに .venv / var / .env を消す(キーは残す)
```

statusline を自作のスクリプトから呼んでいる場合は、そのスクリプトは書き換えない。消す行を表示するので手で消す。

## 確認

```
.venv/bin/python -m usage_forecast.setup status              # この PC の設定状況
.venv/bin/python -m usage_forecast.collector --check-access  # シートへの接続(読み取りのみ)
.venv/bin/python -m usage_forecast.collector --dry-run       # 書き込む予定の内容
bin/run.sh --now                                             # 今すぐ1回実行
python3 -m usage_forecast.session_report --from 10/29 --to 10/31 --hourly   # セッション別の消費
python3 -m usage_forecast.session_report --from 2026-10-01 --backtest       # 異常検知の試し打ち
.venv/bin/python -m pytest -q                                # テスト
```

ログは `var/collector.log`(1MB×5)と `var/probe.log`。

## シート

| シート | 内容 |
|---|---|
| ペース表 | 均等ペース表と予測。「現在の使用量(%)」の B 列だけを自動で上書きする |
| ログ | 観測ごとの1行。使用量、予測、5時間枠、Fable 枠、トークン数(全 PC 合計)、API 換算額 |
| 推移グラフ | 直近14日のグラフ3つ。A〜I 列は collector が毎回作り直す |
| 端末別集計 | PC ごと・週ごとのトークン数と API 換算額。各 PC が自分の行を更新する |
| アラート | 異常検知の記録。1件1行。初めて検知したときに作られる |

## 観測の扱い

- statusline の rate_limits は API 応答のたびにしか更新されない。放置中のセッションは古い値を描画し続ける。
  フックは、セッションごとに「値が変わった」か `cost.total_api_duration_ms` が増えたときだけを新しい観測とみなす。
- 保存済みより低い値は、同じセッションが自分で高い値を見たあとに下がった場合(週途中のリセットやプラン変更)か、
  保存済みの観測が12時間以上前の場合だけ受け入れる。
- ログに書く前に、同じ週の最新行より少しだけ(10ポイント未満)低い値は、他の PC の新しい値より古いとみなして書かない。
- 週のリセットを過ぎたら、ログに「週リセット経過のため無効」を1回だけ書く。
- 週間使用量は、各 PC が30分ごとに読む `/usage` 画面の値(40分以内のもの)を優先する。Web の設定画面と同じ四捨五入の値で、
  Claude Code を使っていない時間も更新される。statusline の値(切り捨て)は、それより新しく、かつ高いときだけ使う。
- 使用量が変わらない間は、ログの行を約1時間に1行に抑える(最新行から55分未満で同じ値なら書かない)。

## 予測

- 平均ペース法: 使用量 ÷ 経過時間 × 168。
- 直近ペース法: 24時間以上前の最新行(なければ週の最初の行)を基準にしたペースで残り時間を延長する。
  10ポイント以上の減少があった場合は、その後の行だけを基準にする。
- グラフでは、週が始まって12時間未満の予測は描かない。

## Fable 枠と従量課金

statusline の JSON には5時間枠と週間・すべてのモデルしか無い。モデル別の週間枠と従量課金(Usage credits)は
`/usage` 画面にしか出ないため、`scripts/usage_probe.py` が tmux で Claude Code を起動して画面を読み取る。

- モデルには何も送らない。Remote Control は起動時に無効にする。所要 5〜15 秒。
- 副作用: `var/probe` フォルダのプロンプト履歴に `/usage` が1行ずつ残る。
- 画面の文言が変わると読み取れなくなる。その場合は該当列が空欄になり、他の記録は続く。

## トークン数と API 換算額

- この PC の Claude Code の会話記録(`~/.claude/projects/**/*.jsonl`)から週の累計を数え、「端末別集計」に書く。
  ログの値は、同じ週の全 PC の行の合計。claude.ai やクラウドのセッションは含まれない。
- 同じ応答が複数行に記録されるので、応答 ID ごとに各項目の最大値を取る。
- API 換算額は `config.json` の `tokens.prices`(API 定価、$/100万トークン)で計算する。サブスクの請求額ではない。
  キャッシュ書き込みは入力の1.25倍(5分)/2倍(1時間)。単価の無いモデルは備考に「単価未設定」と出る。
- 1% あたりの値は「週の累計 ÷ 週間使用量(%)」。使用量が5%未満のときは空欄。
  Claude Code 以外で使った分が多い週は、実際より小さく出る。
- 「上限到達中の消費」は、上限が100%だった時間帯の消費。従量課金がオンなら、その支払い分の目安になる。

## 異常検知

collector は毎回(30分ごと)、次のルールで普段と違う使い方を探す。目安は `config.json` の `anomaly` で変えられる。
金額は API 換算額。

| 種類 | 目安(初期値) | 見ているもの | 通知 |
|---|---|---|---|
| 待機ループ | 1つの会話記録(本体またはサブエージェント)が、直近1時間にキャッシュを200万トークン以上書き直し、出力は1万トークン未満(3回以上の呼び出し) | 完了待ちのポーリングが、キャッシュの寿命(5分)より長い間隔で目を覚まし、そのたびに文脈全体を書き直している | 送る |
| 高額状態の継続 | 1セッション(サブエージェント込み)で1時間 $15 以上が3時間続く | 暴走、止まらない作業 | 送る |
| 急増 | 1セッションで直近1時間 $40 以上 | 短時間の大量消費 | 送る |
| 端末の集計停止 | 他の PC の「端末別集計」が3時間以上更新されていない(7日以上止まっている PC は対象外) | cron、電源、ログイン切れ | 送る |
| 週使用量の急増 | 直近6時間の週使用量の伸びが、均等ペース(100%/168時間)の3倍以上 | Web 版やクラウドも含めたアカウント全体 | 送らない(記録のみ) |

- セッションのルールは、各 PC が自分の会話記録だけを見る。週使用量と端末停止のルールは、共有のシートを見る。
- 検知した内容は「アラート」シートに1行ずつ残し、ステータスラインの末尾に3時間 `⚠ 待機ループ <PC名> $20/h` のように出す。
  ステータスラインには、他の PC が検知したものも出る。
- 同じセッションの同じ種類は24時間に1回、週使用量の急増は12時間に1回だけ記録する。週使用量の急増と端末停止は、
  どの PC が検知しても「アラート」シートで重複を除く。
- 週使用量の急増は、普段の忙しい日にも同じ形になるので、初期値ではスマホには送らない。
  送りたい場合は `anomaly.push_kinds` に `"pace"` を足す。
- 目安を変えるときは、`python3 -m usage_forecast.session_report --from <日付> --backtest` で
  過去の会話記録に当てはめて件数を確かめる。

## 通知

`.env` の `NOTIFY_METHODS` に `smtp`、`webhook`、`ntfy` のどれか(複数ならカンマ区切り)を書く(`.env.example` 参照)。
使用量の閾値は `config.json` の `thresholds`。各レベルは週ごとに1回だけ送る(送った記録は PC ごと)。
異常検知も同じ方法で送る。

### スマホに通知する(ntfy)

1. スマホに ntfy アプリ(iOS / Android)を入れる。アカウント登録は要らない。
2. トピック名を作る。ntfy.sh ではトピック名を知っていれば誰でも読めるので、推測されない長い名前にする:
   ```
   python3 -c 'import secrets; print("cuf-" + secrets.token_urlsafe(18))'
   ```
3. アプリでそのトピックを購読する。
4. 各 PC の `.env` に次を書く(全 PC で同じトピックにする):
   ```
   NOTIFY_METHODS=ntfy
   NTFY_TOPIC=<作ったトピック名>
   ```
5. `bin/run.sh --now` で動かし、`var/collector.log` にエラーが出ていないことを確かめる。

通知の本文には PC 名、種類、金額だけを入れ、プロジェクト名は入れない(「アラート」シートで見る)。
自前の ntfy サーバーを使う場合は `NTFY_SERVER`、アクセス制御付きなら `NTFY_TOKEN` も書く。

## ライセンス

MIT License。`LICENSE` を参照。
