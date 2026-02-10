# PIPLINE_3: OCR → JSONL → 中間加工 → Markdown 生成フロー（更新版）

## 0. 目的
- Active Display First を維持しつつ、**非アクティブ画面のOCRも省略せず保持**する
- stage-01 の特徴抽出は主役（active display）中心、ただし他画面はトレースで保持

## 1. stage-00.raw（生データ保存）
- 入力: `logs/YYYY-MM-DD.jsonl`
- 出力: `trace/<date>/<run_id>/stage-00.raw.jsonl`
- 形式: `{"event": <JSONL event>}`

## 2. stage-01.entities（特徴抽出）
- 目的: 1イベントから “意味抽出に使える特徴量” を作る
- primary_text:
  - `ocr_active_display_text` が使える場合は最優先
  - 使えない場合は `ocr_by_display` の非除外を結合（弱いフォールバック）
- 出力（例）:
  - `event_id`, `ts`, `active_app`, `window_title`, `domain`
  - `segment_key`: `[active_app, domain, window_title]`
  - `primary_source`: `active_display|fallback_all_displays|fallback_empty`
  - `urls`, `paths`（主に primary_text 由来）
  - `ocr_by_display`: `display`, `ocr_text`, `is_active_display`
    - `ocr_text` は改行を空白に置換して正規化

## 3. stage-02.segment（セグメント付与）
- 目的: 連続イベントをまとめてセグメント化する
- セグメントキー（基本）: `(active_app, domain, window_title)`
- 出力:
  - `segment_id`: 連続する同一キーに同じID
  - `segment_label`: `app / domain / window_title` の人間可読ラベル

## 4. stage-03.segment（セグメント内OCR統合）
- 目的: segment_idごとにOCRをまとめ、**重複を削除したOCRテキスト**を作る（LLM前）
- グループ単位: `segment_id`
- 文/行の判定:
  - 句読点（`。.!?！？`）を優先して分割
  - 補助区切り: `▶` / `→` / `・` / `|` / `•`
  - それでも分割できない場合のみ空白で分割
- 重複は segment累積 で除去
- 新規文が0のeventは先頭1文だけ保持
- 頻出文は `common_texts` に分離
- 時間情報:
  - `hour_start_ts`: そのsegmentの最初の `ts`
  - `hour_end_ts`: そのsegmentの最後の `ts`
- 出力:
  - `segment_id`, `segment_key`, `hour_start_ts`, `hour_end_ts`
  - `ocr_by_display`: `display` ごとに `events[]` を持つ
    - `events[]`: `event_id`, `ts`, `is_active_display`, `ocr_text`
    - `ocr_text` は **文/行単位で重複除去**した結果（新規ゼロなら先頭1文を残す）
    - `common_texts` は頻出文（共通ノイズ）を分離したもの

## 5. stage-04.hour-pack（LLM前の1時間パッケージ化 / ルールベース）
- 目的: LLM入力を「1時間」単位で固定し、**背景情報を圧縮**したうえで要約対象を絞る
- 入力: stage-03.segment.jsonl
- 出力（1行=1時間）:
  - `hour_start_ts`, `hour_end_ts`
  - `hour_common_texts`:
    - 各segment/displayの `common_texts` を正規化して近似重複も潰す
    - カウント単位: **(segment_id, display, text)** ごとに1回（同じ画面で出続ける巨大テキストの過大評価を防ぐ）
    - その1時間内の頻出度が高い順に **上位20** を採用
  - `clusters[]`（segment_key単位の塊）:
    - hour内の `segment_key` をまとめ、各clusterに含まれる `segment_ids[]` を保持
    - **作業時間（ts間隔の合計）が長い上位3** のclusterのみを LLM 対象として残す
    - 各clusterは `active_timeline[]`（ts順）を持つ
      - `active_timeline[]`: `ts`, `segment_id`, `ocr_text`（is_active_display=true の eventsのみ。差分中心）

## 6. stage-05.hour-llm（LLM: 1時間要約）
- 目的: `stage-04.hour-pack` を入力として、1時間につき1つの自然文要約を生成する
- LLMの解釈方針:
  - `active_timeline[]` を主入力（時系列の骨）
  - `hour_common_texts` は背景情報として参照（本文への羅列は避ける）
- 出力（例）:
  - `hour_title`, `hour_summary`, `hour_detail`, `confidence`, `evidence`
  - `hour_detail` は自然文で厚め（推測は1つだけ）

## 7. stage-06.daily-llm（LLM: 1日総括）
- 目的: `stage-05.hour-llm`（全hours）を入力として、**1日全体の総括**を生成する
- 出力（例）:
  - `daily_title`, `daily_summary`, `daily_detail`, `highlights[]`, `confidence`, `evidence`

## 8. stage-07.hour-enrich-llm（LLM: 時間帯の目的・意味を踏まえた再解釈）
- 目的: `stage-06.daily-llm`（1日総括）を踏まえて、各時間帯の「目的・意味」を再解釈する
- 入力:
  - `stage-06.daily-llm.json`（daily_context として参照）
  - `stage-05.hour-llm.jsonl`（各時間帯の元の要約）
- 処理の意図:
  - hour-llm は「何をしていたか」（観測ベース）を要約する
  - hour-enrich-llm は「1日全体の目的の中でどのような意味を持つか」を推測する
  - 両方を別々に保持することで、**元の観測と推測を区別**して振り返れる
- 出力（例）:
  - `hour_title_enriched`: 短く具体的（行動+目的）
  - `hour_summary_enriched`: 2〜3文。作業内容+1日全体における意味
  - `confidence`: 推測の確信度（0〜1）
- Markdownへの反映:
  - 元の `hour_title` / `hour_summary` は「主な作業」「概要」として表示
  - `hour_summary_enriched` は「推測される意図」として別項目で表示

## 10. トレース出力（最小セット）
- `run.json`, `stage-00.raw.jsonl`, `stage-01.entities.jsonl`, `stage-02.segment.jsonl`
- 環境変数 `EVERLOG_TRACE_STAGE_MAX=2` で最小セットのみを出力
- `stage-03.segment.jsonl` まで出力したい場合は `EVERLOG_TRACE_STAGE_MAX=3`
- `stage-04.hour-pack.jsonl` まで出力したい場合は `EVERLOG_TRACE_STAGE_MAX=4`
- `stage-05.hour-llm.jsonl` まで出力したい場合は `EVERLOG_TRACE_STAGE_MAX=5`
- `stage-06.daily-llm.json` まで出力したい場合は `EVERLOG_TRACE_STAGE_MAX=6`
- `stage-07.hour-enrich-llm.jsonl` まで出力したい場合は `EVERLOG_TRACE_STAGE_MAX=7`

## 11. 最終マークダウン出力
- 出力先: `EVERYTIME-LOG/out/<date>/<run_id>/<yy-mm-dd_daily_title>.md`
- ファイル名に `daily_title` を含めることで、日報の内容が一目で分かる
- `out/<date>.md` への「latest」コピーは廃止。常に `out/<date>/<run_id>/` に格納する

### 11.1 安全サニタイズ（漏えい対策 / 最終出力のみ）
最終Markdownは基本的にローカルで自分だけが見る想定だが、**万が一漏れた場合**に備えて、
Markdown生成の最終段（`summarize` の書き出し直前）で「共有しても危険になりやすい情報」をローカルでマスクする。

- **適用範囲**: 最終Markdown（および Notion同期で使うタイトル）
- **非適用範囲**: stage-00〜stage-04（ルールベース加工段階）などのローカル中間データ
- **制御**: 環境変数 `EVERLOG_SAFE_MARKDOWN`
  - デフォルト **有効**（未設定でもON）
  - 無効化: `EVERLOG_SAFE_MARKDOWN=0`（互換: `EVERYTIMECAPTURE_SAFE_MARKDOWN=0`）

マスク例（代表）:
- 個人情報/認証情報（メール/電話/カード/OTP/パスワード近傍）
- 典型的なAPIキー/トークン（`sk-...`、GitHub/Slackトークン、JWT 等）
- 秘密鍵ブロック（`BEGIN ... PRIVATE KEY`）