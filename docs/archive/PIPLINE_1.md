

**工程（stage）**
- `stage-00.raw.jsonl`（日次入力の生データ化）
  - 1行=1キャプチャイベント（この run では 564 件、`00:00:11`〜`20:08:06`）。
  - `active_app` / `window_title` / `ocr_text` / `excluded` など「観測そのもの」を保存。

- `stage-01.entities.jsonl`（正規化＋エンティティ抽出＋文脈付与）
  - `ocr_text` を `normalized_text` に整形し、`urls`/`paths`/`keywords`/`snippets` を抽出。
  - `domain` を付け、`segment_key=[app, window_title, domain]` 的なキーで文脈を表現。
  - ここで除外（`excluded` 等）を落とし、以降に回す“有効イベント”だけにします（この run では 495 行）。

- `stage-02.segment.jsonl`（セグメント割当）
  - 名目は「セグメント化」だけど、この run では `stage-01` と内容が完全一致（同一ファイル）で、実質パススルー。
  - `segment_id` と `segment_label`（例: `Cursor` や `Google Chrome / github.com / ...`）がここまでに付いている状態。

- `stage-03.llm.jsonl`（LLMで“作業名/要約/カテゴリ”付与）
  - **有効イベントごと**に `task_title` / `task_summary` / `category` / `confidence` を推定して付与（495行）。
  - これが `out` の「本日のメイン作業（推定）」と「タイムライン」の文言の元ネタになります。

- `stage-04.timeline.jsonl`（セグメント単位のタイムライン文生成）
  - `segment_id` 単位にイベントをまとめて、開始/終了時刻レンジ＋所要時間を計算。
  - 代表の `task_title/task_summary/category` を使って、1セグメント=1行のMarkdown行（この run では 101 行）を生成。
  - `out` の「タイムライン（推定）」は基本これを貼っています（文言は少し整形されることあり）。

**`out`（日次Markdown）でやっている集計（stageファイルから合成）**
- ヘッダ統計（記録期間、推定総時間、キャプチャ数の内訳）
  - 記録期間: `stage-00` の最初/最後の `ts` から。
  - 有効/除外: `stage-00` の `excluded` と、`stage-01` に残った件数を使っているはず（※この run だと `stage-01=495` なので、`out` の「有効 496」とは 1件ズレがあります）。
  - 推定総時間: `segment` やイベントの `interval_sec` を足し上げ（「有効分のみ」）。

- 「本日のメイン作業（推定）」
  - `stage-03.llm` の `task_title` を軸に、近い時間帯のセグメント/イベントを束ねて時間を合算し、上位N件を出力。

- 「アプリ使用状況（推定）」テーブル
  - `stage-02` の `segment_label`（アプリ＋ドメイン等の文脈ラベル）ごとに滞在時間/回数を集計して表にする。

- 「タイムライン（推定）」
  - `stage-04.timeline` の行を列挙。

**1時間ダイジェストについて**
- この `200910-047143` のトレースには `stage-03.hour.jsonl` / `stage-04.hour-llm.jsonl` が無いので、あなたの言う「1時間ダイジェストがない時代（少なくともこの run のパイプラインでは未導入）」で合っています。
- 一方で同日の別run（例: `trace/2026-02-05/233401-166810`）には `stage-03.hour.jsonl` と `stage-04.hour-llm.jsonl` があるので、後で「時間集計→時間LLM要約」の工程が追加されています。

次はどれをやるのが目的に近いですか？
- `out` の「有効 496」vs `trace` の 495 のズレ理由だけ特定する
- `233401-166810` と `200910-047143` の `out` を見比べて、“いつ何が増えたか”を差分で整理する