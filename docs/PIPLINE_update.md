# このドキュメントでやりたいことと周辺情報
このプロジェクトの全体感をまず読み取ってください。 @docs/DESIGN.md や @README.md にあります
そのうえでパイプライン処理の変更を行いたいのがこのドキュメントの主旨です。

現状のパイプラインは @docs/PIPELINE_2.md になっていて、その解説は @docs/PIPLINE_2_解説.md に載っています。
また、中間データは /Users/arima/DEV/everytimecapture/EVERYTIME-LOG/trace で追えるようになっているし詳細も @EVERYTIME-LOG/trace/README.md にのってます。

@docs/PIPLINE_2_解説.md や @docs/PIPELINE_2.md は過去のパイプライン集なので編集しないで。
一方で、 @EVERYTIME-LOG/trace/README.md は変更していって欲しい。
新たなパイプラインは PIPLINE_3として新たにドキュメントを作成してそこにまとめていって欲しい。


指示に不明点や考慮不足があれば質問してください。チャットは日本語でやりとりしてください。

では、stage01から順に始めます。

# stage01の課題点

## 1. アクティブではないディスプレイの情報が抹消されている
### 課題の詳細
@docs/PIPELINE_2.md  に「各イベント（1キャプチャ）で、ocr_active_display_text を最優先の入力テキストとする。なぜ: 全ディスプレイOCRを結合すると、関係ない文字列が混ざり「フラグ過多」「羅列」になりやすい。」と記載がある影響で、アクティブ以外のディスプレイ情報がref_urlsとref_snippetsにまとめられてしまっています。

### どうしたいか
* activeではないディスプレイのOCR結果はref_urlsやref_snippetsのように省略せず、元のログのままdisplayとOCRテキストは削除せずに残しておく。なお、OCRテキストの改行を空白に置換して正規化することはactiveではないディスプレイに対しても実施すること。その他の不要フィールドは削除する方針のまま継続で良い。

* activeなディスプレイとその情報は特別扱いしたいため、activeなディスプレイ（Appではなくディスプレイ）のOCRテキストはactiveなディスプレイであるという情報を持とう。

* ref_urlsやref_snippetsは逆にノイズになるので削除する

* stage-01 の出力には `ocr_by_display` を残し、各要素は `display`, `ocr_text`, `is_active_display` のみを保持する
* stage-01/02 から `normalized_text` を廃止し、`ocr_by_display` を主データとして後工程に渡す


# stage02の課題点
stage02に特に課題はありません。処理内容としては、引き続き同じsegment_keyをもつ連続イベントに同じIDを付与し、segment_labelとして人間可読なラベル（app / domain / window_title）にする処理を行なってください。ただし、たった今手を加えたstage01に応じて変更すべき部分があれば変更してください。

# 中間確認
stage02の修正が終了したら、stage00~stage02までを一度出力してみてください。格納先はtrace配下に指定した形式でいれ、run,00,01,02の4つのファイルのみで現時点では問題ありません。

# 実装メモ
* 新パイプラインは `docs/PIPLINE_3.md` にまとめる
* `EVERLOG_TRACE_STAGE_MAX=2` で `run.json` と stage-00/01/02 のみ出力


# stage-03の課題点
## 1.snipettsやsignalなどの単語レベルでしか出てこなくなる
### 課題の詳細
同一セグメントIDでまとめたあとにすぐ1時間単位でまとめていて、ほぼ単語レベルだけになってしまっており肝心のOCRテキストが消え去って文脈が大幅に排除されている

### どうしたいか
* event_idごとで同一セグメントIDをグループ化する。このとき、それぞれのディスプレイの個別性やアクティブ判定は維持し続ける。各ディスプレイのOCR読み取り結果に対して、同一セグメントIDで重複している文章がある箇所はまとめて1つの文章にしたい。文章の単位はocrテキストの空白から空白までの間を1チャンクとする。このチャンク判定が空白ではなくカンマなど別の方が良さそうな場合は提案して欲しい。重複文章を1文章にまとめたうえで最終的にどのようなocr_textにまとめるかは悩みどころではあるが、なるべく文脈が壊れないように、重複していない箇所についてはevent_idごとにある程度固まって保持されるようにして欲しい。

* この段階では1時間にまとめない

## 2.llmで処理するデータ数が多い
### 課題の詳細
LLMがセグメント内のOCRテキストを解析していたが、重複している文言が非常に多くボリュームが増えてしまっている。

### どうしたいか
* 課題1の解決策を実施し、llmをかける前にまず重複を削除したセグメントグループのocrテキストを作成する（ディスプレイの個別性やアクティブ判定は維持）。つまりこの工程＝stage03ではllmはかけない。

* llmは次のstage-04で実施とする

## 3.時間情報が消えやすい
### 課題の詳細
グルーピングおよび1時間にまとめた際に、時間情報が消えてしまっている。

### どうしたいか
* 同一セグメントIDをグループ化する際にどのセグメントIDがtsからどのtsまでの出来事なのかを記録し、セグメントIDごとに時間間隔を新たに持つ
* 同一セグメントIDごとに"hour_start_ts"と"hour_end_ts"みたいな形でもてるといいかも

## その他の指示
* この工程はstage-03と呼称する（stage-03.hour.jsonl にしない）
* stage02まであった"segment_key"の情報はstage03でも維持
* 元々stage-03-hoursにあったtop_apps・top_domains・context・signals・snippetsはこの工程では廃止。transitionsもこの工程では不要になる認識。

stage03では、同一segment_idごとにグルーピング化されており、segment_keyの情報やディスプレイの個別性やis_active_displayの情報は維持する。その上でグルーピングに際して"hour_start_ts"と"hour_end_ts"の時間間隔を保持し、各ocr_textはある程度のまとまりで重複が判定され重複文章は1文章にまとめっていて、なるべく文脈が壊れないように、重複していない箇所についてはevent_idごとにある程度固まって保持されており、top_appsやtop_domainsやcontextはなくなっているしllmにもかけられていない。元々stage-03-hoursにあったtop_apps・top_domains・context・signals・snippetsはこの工程では廃止。transitionsもこの工程では不要になる認識。

## stage-03 のルール（確定）
- 文/行分割の優先順: 句読点 → 補助区切り（▶/→/・/|/•）→ 空白
- 重複は segment累積 で除去
- 新規文が0のeventは先頭1文だけ保持
- 頻出文は common_texts に分離

## stage-04（LLM）への引き継ぎ意図
- LLMは `events[]` と `common_texts` を合わせて参照し、**各eventで何をしていたか**を推測する
- `events[]` は差分中心（新規文）なので、`common_texts` を背景情報として使う前提で解釈させる
- `is_active_display=true`の`ocr_text`が作業中のディスプレイであり最も重要。`is_active_display=false`は作業中に参照している情報と捉えること。

# stage-04の方針


## 方針
* stage-03までは課題起点だったがstage-04以降はstage03までの情報を踏まえて新たに再構築する。
* stage-03の出力（segment単位の events/common_texts）をそのまま全segmentにLLMを掛けず、1時間単位にパッケージ化して背景情報を圧縮し、重要な作業だけをLLM入力にする。

### stage-04（ルールベース / hour-pack）
- stage-03を1時間ごとにまとめ、LLM入力パッケージを作る（この工程ではLLMを使わない）
- `hour_common_texts`:
  - stage-03の `common_texts` を時間帯内で集計
  - 正規化して近似重複を潰した上で、**頻出度の高い上位20** を採用
- `clusters[]`（segment_key単位の塊）:
  - hour内の `segment_key` をまとめ、clusterに `segment_ids[]` を保持
  - **作業時間（ts間隔合計）が長い上位3** のclusterだけをLLM入力に採用
  - `active_timeline[]`（ts順）を保持し、入力の時系列の骨にする
  - この段階で `is_active_display=false` のeventsは落とし、**activeだけ**を主データとして残す

### stage-05（LLM / hour-llm）
- stage-04の1時間パッケージに対して、**1時間につき1つの自然文要約**を生成する
- LLMは `active_timeline[]` を主入力（時系列の骨）、`hour_common_texts` を背景として参照する
- 出力は「何をしていたか」（**観測ベース**）

### stage-06（LLM / daily-llm）
- stage-05の全時間帯の要約を入力として、**1日全体の総括**を生成する
- `daily_title`, `daily_summary`, `highlights[]` などを出力

### stage-07（LLM / hour-enrich-llm）
- stage-06の1日総括（daily_context）を踏まえて、各時間帯の「目的・意味」を再解釈する
- 入力:
  - `stage-06.daily-llm.json`（daily_context として参照）
  - `stage-05.hour-llm.jsonl`（各時間帯の元の要約）
- 出力:
  - `hour_title_enriched`: 短く具体的（行動+目的）
  - `hour_summary_enriched`: 2〜3文。「何をしていたか」+「1日全体における意味」
  - `confidence`: 推測の確信度（0〜1）
- 設計意図:
  - hour-llm は「何をしていたか」（観測ベース）
  - hour-enrich-llm は「1日全体の目的の中でどのような意味を持つか」（推測ベース）
  - 両方を別々に保持することで、**元の観測と推測を区別**して振り返れる
  - LLMの推測が実際の意図と異なる場合でも、元の観測から作業内容を思い出せる

### 最終マークダウン
- 出力先: `EVERYTIME-LOG/out/<date>/<run_id>/<yy-mm-dd_daily_title>.md`
- ファイル名に `daily_title` を含めることで、日報の内容が一目で分かる
- `out/<date>.md` への直接出力は廃止。常に `out/<date>/<run_id>/` に格納する
- タイムラインの構成:
  - **主な作業**: `hour_title`（hour-llmの出力 / 観測ベース）
  - **概要**: `hour_summary`（hour-llmの出力 / 観測ベース）
  - **推測される意図**: `hour_summary_enriched`（hour-enrich-llmの出力 / 推測ベース）