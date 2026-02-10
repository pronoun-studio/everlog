# 2026-02-06 stage-02 → (active OCR抽出) → (event要約 + 非active参照) → (hour timeline) → Markdown 実験

目的: **2/6の `stage-02.segment.jsonl` を使い、非アクティブ画面を参照しながらアクティブ画面の作業を要約した場合の出力/コスト**を確認する。

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
- 入力: `EVERYTIME-LOG/trace/2026-02-06/*/stage-02.segment.jsonl`
  - デフォルトは **「ファイル更新日時が最新」** の run を採用
  - run を固定したい場合は `--run-id` を指定

---

## 実行（段階実行）

出力はこのフォルダ内の `outputs/` に作られます。

### 0) 入力(stage-02)の自動選択を確認

```bash
python3 experiments/exp_2026-02-06_gpt-5-nano_active-context/run_pipeline.py find-stage02 --date 2026-02-06
```

### 1) アクティブディスプレイのOCRだけ抽出

```bash
python3 experiments/exp_2026-02-06_gpt-5-nano_active-context/run_pipeline.py extract-active-ocr --date 2026-02-06
```

### 2) eventごとに要約（LLM呼び出し）

```bash
python3 experiments/exp_2026-02-06_gpt-5-nano_active-context/run_pipeline.py summarize-events --date 2026-02-06 --model gpt-5-nano
```

- 非アクティブディスプレイのOCRは **参照情報として** 使います
- 途中停止しても再開できます（既に要約済み `event_id` はスキップ）
- 追加でコストを下げたい場合は `--batch-size` や `--max-chars` を下げてください

### 3) 1時間ごとのタイムライン化（ルールベース）

```bash
python3 experiments/exp_2026-02-06_gpt-5-nano_active-context/run_pipeline.py build-hourly --date 2026-02-06
```

### 4) Markdown生成（最終）

```bash
python3 experiments/exp_2026-02-06_gpt-5-nano_active-context/run_pipeline.py render-md --date 2026-02-06
```

生成物:

- `experiments/exp_2026-02-06_gpt-5-nano_active-context/outputs/04.report.md`

---

## 最終Markdownの構成（3セクション）

以下の **3つの `##` 見出し**で出します:

1. `## 処理コスト`
2. `## 本日のメイン作業`
3. `## 1時間ごとのタイムライン`

