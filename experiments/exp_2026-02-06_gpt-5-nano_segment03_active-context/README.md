# 2026-02-06 stage-03 → (segment LLM) → (hour-llm-β) → (daily) → Markdown 実験

目的: **2/6の `stage-03.segment.jsonl` を使い、アクティブ画面のOCRを主、非アクティブ画面を参照情報としてセグメント要約し、hour-llm-β と daily-llm を経由して最終Markdownを作る**。

この実験は本体パイプラインを汚さないように、`experiments/` 配下で完結します。

---

## 使うモデル

- **`gpt-5-nano` 固定**

（コスト計算は `everlog/llm.py` の `calc_cost_usd()` を利用します）

---

## 前提

- Python: `python3` が使えること
- 環境変数 `OPENAI_API_KEY` があること
  - 無い場合でも `everlog/llm.py` と同じ仕組みで `.env` を探索します
- 入力: `EVERYTIME-LOG/trace/2026-02-06/*/stage-03.segment.jsonl`
  - デフォルトは **「ファイル更新日時が最新」** の run を採用
  - run を固定したい場合は `--run-id` を指定

---

## 実行（段階実行）

出力はこのフォルダ内の `outputs/` に作られます。

### 0) 入力(stage-03)の自動選択を確認

```bash
python3 experiments/exp_2026-02-06_gpt-5-nano_segment03_active-context/run_pipeline.py find-stage03 --date 2026-02-06
```

### 1) セグメント単位でOCRを集約（active+inactive）

```bash
python3 experiments/exp_2026-02-06_gpt-5-nano_segment03_active-context/run_pipeline.py extract-segment-ocr --date 2026-02-06
```

### 2) segmentごとに要約（LLM呼び出し）

```bash
python3 experiments/exp_2026-02-06_gpt-5-nano_segment03_active-context/run_pipeline.py summarize-segments --date 2026-02-06 --model gpt-5-nano
```

- **active_display_ocr_text が主作業**
- **inactive_display_ocr_text は参照情報**
- 「非アクティブなディスプレイを参照しながらアクティブなディスプレイの作業をしている」文脈で要約

### 3) hour-llm-β（segment要約 + hour-pack）

```bash
python3 experiments/exp_2026-02-06_gpt-5-nano_segment03_active-context/run_pipeline.py hourly-llm-beta --date 2026-02-06 --model gpt-5-nano
```

### 4) daily-llm

```bash
python3 experiments/exp_2026-02-06_gpt-5-nano_segment03_active-context/run_pipeline.py daily-llm --date 2026-02-06 --model gpt-5-nano
```

### 5) Markdown生成（最終）

```bash
python3 experiments/exp_2026-02-06_gpt-5-nano_segment03_active-context/run_pipeline.py render-md --date 2026-02-06
```

---

## まとめて実行

```bash
python3 experiments/exp_2026-02-06_gpt-5-nano_segment03_active-context/run_pipeline.py all --date 2026-02-06 --model gpt-5-nano
```

---

## 生成物

- `01.segment_ocr.jsonl` (stage-03からのOCR集約)
- `02.segment_summaries.jsonl` (segment-llmの結果)
- `02.usage_calls.jsonl` (segment-llmのusageログ)
- `03.hourly_llm.json` (hour-llm-β出力)
- `04.daily_llm.json` (daily-llm出力)
- `05.report.md` (最終Markdown)

---

## 最終Markdownの構成（3セクション）

以下の **3つの `##` 見出し**で出します:

1. `## 処理コスト`
2. `## 本日のメイン作業`
3. `## 1時間ごとのタイムライン`
