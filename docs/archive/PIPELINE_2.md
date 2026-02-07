<!--
Role: OCR→JSONL→中間加工→Markdown生成の「処理パイプライン」を、実装に落とせる粒度で固定する。
How: ステージ（trace）単位の入出力・主要ロジック・LLM方針（低トークン）・active display優先方針を明記する。
Collaboration: 仕様の正は DESIGN.md。実装俯瞰は ARCHITECTURE.md。除外/マスクは EXCLUSIONS.md。
-->
# OCR → JSONL → 中間加工 → Markdown 生成フロー（Active Display First / 1時間タイムライン）

## 0. 目的
- OCRの一次情報（意味の通る文章）を活かしつつ、Markdownのタイムラインを「読み物として自然」にする
- **主役はアクティブ中の画面**（active display）。他ディスプレイは参考情報として扱い、本文に混ぜすぎない
- LLMは任意かつ低回数（コスト/漏えいリスクを抑える）

## 1. 前提（重要）
### 1.1 Active Display First
各イベント（1キャプチャ）で、`ocr_active_display_text` を最優先の入力テキストとする。

- 使う条件: `ocr_active_display_excluded == False` かつ `ocr_active_display_text` が非空
- 条件を満たさない場合はフォールバック（後述）し、**推測のトーンも下げる**

なぜ: 全ディスプレイOCRを結合すると、関係ない文字列が混ざり「フラグ過多」「羅列」になりやすい。

### 1.2 LLMは“本文”と“根拠”を分離する
- 本文（hour_summary/hour_detail）は自然文優先（URL/パスの羅列は禁止）
- 根拠（evidence）は `<details>` に隔離して「見たい人だけ見る」

## 2. 入力（JSONL Event）: このパイプラインが参照するフィールド
最低限（既存）:
- `id`, `ts`, `tz`, `interval_sec`
- `active_app`, `window_title`
- `browser.domain`（取れる場合）
- `ocr_by_display[]`（display別OCR、excluded含む）

Active display関連（v0.2拡張・推奨）:
- `active_display` / `active_display_source` / `active_display_error`
- `ocr_active_display_text`
- `ocr_active_display_excluded` / `ocr_active_display_excluded_reason`

## 3. 全体フロー（Mermaid）
```mermaid
graph TD
  CAP[Capture: screencapture + OCR] -->|1行=1イベント| JSONL[logs/YYYY-MM-DD.jsonl]

  JSONL --> S00[stage-00.raw: event]
  S00 --> S01[stage-01.entities: active-first特徴抽出]
  S01 --> S02[stage-02.segment: セグメント化]
  S02 --> S03[stage-03.hour: 1時間ダイジェスト]
  S03 -->|optional| S04[stage-04.hour-llm: 1時間LLM要約]
  S03 -->|no-llm| MD[Markdown生成]
  S04 --> MD[Markdown生成]

  MD --> OUT[out/YYYY-MM-DD(.md)]
```

## 4. ステージ（trace）定義
この章は `EVERLOG_TRACE=1` 時に「どの中間生成物が何か」を追えるようにするための“契約”。

### stage-00.raw（現行互換）
- 目的: JSONLイベントをそのまま保存（デバッグ用）
- 形式（例）: `{"event": <JSONL event>}`

### stage-01.entities（active-first）
- 目的: 1イベントから “意味抽出に使える特徴量” を作る（OCR全文は使わない/送らない方向）
- 入力テキスト:
  - `primary_text`: `ocr_active_display_text`（最優先）
  - `reference_text`: `ocr_by_display` のうち `excluded=false` を結合（補助。使う量は後段で絞る）
- 出力（例）:
  - `event_id`, `ts`, `active_app`, `window_title`, `domain`
  - `primary_source`: `active_display|fallback_all_displays|fallback_empty`
  - `urls`, `paths`, `keywords`, `snippets`（原則 primary_text 由来）
  - `ref_urls`, `ref_snippets`（reference_text 由来。少量）

### stage-02.segment（現行互換 + active-first）
- 目的: 近いコンテキストのイベントをまとめて segment を作る
- セグメントキー（基本）: `(active_app, domain, window_title)`
  - `window_title` が空のときは空のまま扱う（無理にOCRから推定しない）
- 付与:
  - 各eventに `segment_id` / `segment_label`
  - segment集約に使う `keywords/snippets` は primary由来を優先

### stage-03.hour（新規）
- 目的: 1時間ごとに「観測→推測」に必要なダイジェストを作る（LLM入力を小さくする）
- 出力（1行=1時間ブロック）:
  - `hour_start_ts`, `hour_end_ts`
  - `active_sec_est`（推定稼働時間）
  - `top_apps`, `top_domains`, `top_contexts`（最大6）
  - `signals`（最大12）: URL/ファイル/コマンド/エラー見出し等（短縮済み）
  - `snippets`（最大6）: “意味のある行”のみ（UIノイズ除去済み）
  - `transitions`（最大4）: この時間帯の主要な画面遷移
  - `evidence_ref`（最大8）: 他ディスプレイ由来の参考（details行き）

### stage-04.hour-llm（新規・必須）
- 目的: 1時間ダイジェストに自然言語の推測を付与（本文と根拠を分離）
- 実行条件（例）:
  - `active_sec_est >= 120`（2分以上稼働の時間だけ）
  - 1日最大24回（さらに上限を設けてもよい）
- **このステージが無い場合は「未完成品」として扱う（LLMなしのタイムラインは完成扱いしない）**
- 出力（JSON）:
  - `hour_title`（短い作業名）
  - `hour_summary`（1〜2文。URL/パス羅列禁止）
  - `hour_detail`（3〜8文。観測→推測）
  - `screens[]`（最大5）: “どの画面に何が書いてあった→何を示唆” を明示
  - `category`, `confidence`
  - `evidence[]`（最大8）: URL/ファイル名/コマンド等（details行き）

## 5. 中間加工A（stage-01）: snippets の抽出ロジック
### 5.1 primary_text の決定
1) `ocr_active_display_excluded == False` かつ `ocr_active_display_text` 非空 → primary
2) それ以外で、`ocr_by_display` に `excluded=false` がある → `reference_text` を primary扱い（ただし弱い）
3) それでも空 → primaryなし（snippets空）

`primary_source` に `active_display|fallback_all_displays|fallback_empty` を入れて後段が判断できるようにする。

### 5.2 候補生成（強アンカー優先）
primary_text から以下を抽出し、まず候補に入れる（行より強い）:
- URL（`host/path` へ正規化、クエリは原則捨てる）
- POSIXパス（`/Users/<name>/...` → `~/...`、長い場合は末尾寄せ）
- ファイル名トークン（`*.py`, `*.md`, `*.jsonl`, `*.app` など）
- コマンド断片（例: `2>&1`）

### 5.3 行候補（line）とUIノイズ除去
OCRを改行で分割し、行ごとにスコアリングする。

汎用ノイズ（アプリ不問で落とす/強減点）:
- 記号だけ、短すぎ（1〜2文字）、数字だけ
- タブ/×/✕ が異常に多い（タブバーやUI列）
- ほぼメニュー列だけ（例: `File Edit View ...`）

“強アンカーが無ければ”減点（ファミリ/ドメインで少数だけ持つ）:
- `domain == chatgpt.com` 系の定型UI語（新しいチャット/ライブラリ等）
- `browser系` の戻る/進む/共有などのUI語
- `IDE系` のメニュー列（上と被るが、より厳しめに）

重要: ここは「削除」ではなく「減点」。アンカー（URL/パス/エラー語）が強い行は救済する。

### 5.4 採用（最大N・多様性）
上位からN個を単純に取らず、次の多様性を優先する:
- URL系を1つ
- パス/ファイル系を1つ
- 自然文/エラー行/コマンド行を1〜2つ
残りはスコア順。

### 5.5 1時間集約での“頻出ペナルティ”
event単体では残りやすいUI行を抑えるため、stage-03で以下を適用:
- 1時間内で同一snippetが頻出なら減点（=背景ノイズ）
- その時間にだけ出たレアsnippetを優先（=その時間の特徴）

## 6. Markdown生成（出力仕様の骨子）
### 6.1 1時間タイムライン（主）
各時間ブロックで:
- 見出し: `### 22:00–23:00（推定稼働: 18分）`
- 本文: `hour_summary` / `hour_detail`（自然文）
- 画面内訳（screens）: 2〜5点
- `<details>`: `evidence` / `evidence_ref`

### 6.2 本日のメイン作業（TOP3）
入力は hour ブロックの `hour_title` と `active_sec_est`。
- まずルールでクラスタ（同一repo/同一ドメイン/同一アプリなど）
- 必要なら最後にLLM 1回でタイトル整形（任意）

## 7. 互換性（古いJSONL）
- `ocr_active_display_text` が無いログは `ocr_by_display` の非除外を結合して primary扱い（ただし弱い）
- `ocr_by_display` が無い場合は `ocr_text` を使う（さらに弱い）
- 互換時は `primary_source` に明示し、推測の確信度を下げる

## 8. 実装への落とし込み（最小変更の当たり所）
active-firstを効かせる最小変更はここ:
- `everlog/segments.py` のイベントOCR取得を `ocr_active_display_text` 優先にする
  - これだけで `segments` / `enrich` / `summarize` の入力が主役寄りになる

1時間タイムラインは追加実装（別途）:
- hourダイジェスト生成（stage-03）
- hour LLM（stage-04、任意）
- Markdownテンプレ更新
