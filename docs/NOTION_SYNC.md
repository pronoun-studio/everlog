# Notion同期設計書

## 概要

毎日生成されるマークダウン（日報）をNotionデータベースに自動同期する機能の設計。

---

## 設計方針

### 同期方法: summarize実行時 + リトライ機構

```
┌─────────────────────────────────────────────────────────────────┐
│  summarize.py                                                    │
│  └── Markdown生成完了                                            │
│       ↓                                                          │
│  notion_sync() を呼び出し                                        │
│       ├── 成功 → done                                            │
│       └── 失敗 → pending状態を記録                               │
│                   (~/.everlog/notion_pending.json)               │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│  次回summarize実行時                                             │
│  └── pending.json をチェック                                     │
│       └── 未同期があれば再試行（成功したらpendingから削除）       │
└─────────────────────────────────────────────────────────────────┘
```

### 決定事項

| 項目 | 決定内容 |
|---|---|
| 同期タイミング | summarize実行時 |
| リトライ | 次回summarize時に未同期を再試行 |
| 同期内容 | **全文マークダウン** |
| Notion構造 | Database（1行 = 1日） |
| DB新規/既存 | 既存DBへの統合も可能 |

---

## アーキテクチャ

### ファイル構成

```
everlog/
├── notion_sync.py      # 新規作成
│   ├── sync_to_notion()       # メイン同期処理
│   ├── mark_pending()         # 失敗時にpending記録
│   ├── retry_pending()        # 未同期の再試行
│   ├── remove_pending()       # 成功時にpending削除
│   └── md_to_notion_blocks()  # Markdown→Notionブロック変換
│
└── summarize.py        # 既存を修正
    └── 末尾でnotion_sync呼び出し（環境変数で有効化）
```

### 状態管理ファイル

`~/.everlog/notion_pending.json`:

```json
{
  "pending": [
    {
      "date": "2026-02-07",
      "run_id": "22-58-1",
      "md_path": "/Users/.../26-02-07_xxx.md",
      "daily_llm_path": "/Users/.../2026-02-07.daily.llm.json",
      "failed_at": "2026-02-07T23:55:30+09:00",
      "retry_count": 0,
      "last_error": "Network unreachable"
    }
  ]
}
```

---

## Notionデータベース設計（ALL-DB連携）

### 使用するプロパティ（列）

| プロパティ名 | 型 | Everlogからの値 | 備考 |
|---|---|---|---|
| `活動ログ` | Title | daily_title（日報タイトル） | メイン |
| `実施日_編集可` | Date | 日付（2026-02-07など） | 重複チェック用 |
| `ジャンル` | Multi-select | `AutoLog`（固定値） | 自動同期を識別 |

### ページ本文

- 全文マークダウンをNotionブロックとして変換
- 見出し → `heading_1`, `heading_2`, `heading_3`
- リスト → `bulleted_list_item`, `numbered_list_item`
- テーブル → `code`（簡易対応）
- 通常テキスト → `paragraph`

---

## 同期フロー詳細

### 1. summarize完了時

```python
def summarize_day_to_markdown(date: str):
    # ... 既存のMarkdown生成処理 ...
    
    # Notion同期（環境変数で有効化）
    if os.environ.get("EVERLOG_NOTION_SYNC") == "1":
        from .notion_sync import sync_daily, retry_pending
        
        # まず未同期があれば再試行
        retry_pending()
        
        # 今回分を同期
        sync_daily(
            date=date,
            run_id=run_id,
            md_path=md_path,
            daily_llm_path=daily_llm_path
        )
```

### 2. sync_daily() の処理

```python
def sync_daily(date: str, run_id: str, md_path: Path, daily_llm_path: Path):
    try:
        # 1. daily_llm.json を読み込み（プロパティ用）
        daily_llm = load_json(daily_llm_path)
        
        # 2. マークダウンを読み込み
        md_content = md_path.read_text()
        
        # 3. 既存ページを検索（date重複チェック）
        existing_page = find_page_by_date(date)
        
        # 4. upsert（更新 or 新規作成）
        if existing_page:
            update_page(existing_page["id"], daily_llm, md_content)
        else:
            create_page(daily_llm, md_content)
        
        # 5. 成功したらpendingから削除（あれば）
        remove_pending(date)
        
    except Exception as e:
        # 失敗時はpendingに記録
        mark_pending(date, run_id, md_path, daily_llm_path, str(e))
```

### 3. retry_pending() の処理

```python
def retry_pending():
    pending = load_pending()
    for item in pending["pending"]:
        # 日付ごとに再試行（最新のrun_idのみ）
        try:
            sync_daily(
                date=item["date"],
                run_id=item["run_id"],
                md_path=Path(item["md_path"]),
                daily_llm_path=Path(item["daily_llm_path"])
            )
            # 成功したら自動的にpendingから削除される
        except Exception:
            # 失敗時はretry_countを増加して再記録
            item["retry_count"] += 1
            save_pending(pending)
```

---

## 環境変数

```bash
# .env に追加

# Notion API（Integration Token）
NOTION_API_KEY=ntn_xxxxxx...

# 同期先データベースID（ALL-DBの場合の例）
NOTION_DATABASE_ID=25ab3850f8c38153b303c6c408d7cd4e

# 同期機能の有効化（1で有効）
EVERLOG_NOTION_SYNC=1
```

### 現在の設定（ALL-DB連携）

- **Database**: ALL-DB
- **Integration**: whinny
- **同期タイミング**: summarize実行時（23:55のlaunchd実行時など）
- **識別**: `ジャンル = AutoLog` でフィルタリング

---

## Notion側の準備手順

### 1. Integration作成

1. https://www.notion.so/my-integrations にアクセス
2. 「New integration」をクリック
3. 設定:
   - Name: `Everlog Sync`
   - Type: Internal integration
   - Capabilities: Read content, Update content, Insert content
4. 「Submit」→ **Internal Integration Secret** をコピー
5. `.env` の `NOTION_API_KEY` に設定

### 2. Database準備

**新規作成の場合:**
1. Notionでフルページデータベースを作成
2. 上記プロパティを追加

**既存DBを使う場合:**
1. 対象DBを開く
2. 上記プロパティを追加（既存と被らないように）

### 3. IntegrationをDBに招待

1. データベースページを開く
2. 右上「...」→「Connections」→「Connect to」
3. 作成したIntegration（`Everlog Sync`）を選択

### 4. Database IDを取得

- ブラウザでデータベースを開く
- URL例: `https://www.notion.so/myworkspace/abc123def456...?v=xyz`
- `abc123def456...` の32文字部分が Database ID
- `.env` の `NOTION_DATABASE_ID` に設定

---

## Markdown → Notion ブロック変換

### 変換ルール

| Markdown | Notion Block Type |
|---|---|
| `# Heading` | `heading_1` |
| `## Heading` | `heading_2` |
| `### Heading` | `heading_3` |
| `- item` | `bulleted_list_item` |
| `1. item` | `numbered_list_item` |
| `> quote` | `quote` |
| `` `code` `` | `code`（inline） |
| ` ```code``` ` | `code`（block） |
| `**bold**` | rich_text with bold annotation |
| `*italic*` | rich_text with italic annotation |
| 通常テキスト | `paragraph` |
| テーブル | `table` または `paragraph`（簡易） |

### 実装方針

- 既存ライブラリ（`markdown-it` / `mistune` など）でパース
- Notion API の block 形式に変換
- 複雑な要素（テーブル等）は段階的に対応

---

## エラーハンドリング

### 想定されるエラー

| エラー | 原因 | 対処 |
|---|---|---|
| Network unreachable | ネットワーク未接続 | pending記録 → 次回リトライ |
| 401 Unauthorized | API Key無効 | ログ出力、pending記録 |
| 404 Not Found | Database ID無効 | ログ出力、pending記録 |
| 400 Bad Request | プロパティ不一致 | ログ出力、詳細エラー表示 |
| Rate Limit | API制限 | リトライ時に対応 |

### リトライ上限

- `retry_count` が 5 を超えたらログ警告
- 手動対応を促す（pendingからは削除しない）

---

## CLI拡張（将来）

```bash
# 手動同期
everlog notion sync --date 2026-02-07

# pending確認
everlog notion status

# pending強制再試行
everlog notion retry

# pending削除
everlog notion clear --date 2026-02-07
```

---

## 実装状況

### ✅ Phase 1: 基本同期（完了）
1. ✅ `notion_sync.py` 作成（sync_daily, mark_pending, retry_pending）
2. ✅ `summarize.py` への統合
3. ✅ 動作確認（2026-02-06, 2026-02-07で検証済み）

### ✅ Phase 2: Markdown変換（完了）
1. ✅ 基本要素の変換（見出し、リスト、段落）
2. ✅ テーブル対応（code blockとして変換）
3. ✅ 番号付きリスト対応

### 📋 Phase 3: CLI拡張（将来）
1. 手動同期コマンド
2. ステータス確認コマンド

---

## Notionオートメーション対応

### 背景

Notionでは、ページ作成時に自動的に「作成日」プロパティが設定される。
ALL-DBではオートメーションにより「作成日 → 実施日_編集可」への転記が行われる。

このため、例えば2/7のログを2/8にアップロードすると：
1. 作成日 = 2/8 が設定される
2. オートメーションで「実施日_編集可 = 2/8」に転記される
3. 本来の日付（2/7）が上書きされてしまう

### 対策: 遅延更新

```python
def _fix_date_after_automation(page_id: str, date: str, wait_seconds: int = 60):
    """Notionオートメーション後に実施日_編集可を正しい日付に再設定する"""
    time.sleep(wait_seconds)  # オートメーションを待機
    # 実施日_編集可 を正しい日付で再設定
    _notion_request("PATCH", f"/pages/{page_id}", {
        "properties": {"実施日_編集可": {"date": {"start": date}}}
    })
```

### フロー

```
1. ページ作成/更新
2. [60秒待機] オートメーションが完了するのを待つ
3. 実施日_編集可 を正しい日付で再設定
```

### 待機時間の調整

環境変数やConfigで調整可能にすることも検討中。
現在は60秒固定（オートメーションには十分な時間）。

---

## 既知の制限事項

### タイムゾーン
- NotionはUTCで日付を保存するため、JSTの日付は+1日ずれて保存されることがある
- 対策: 検索時に date と date+1日 の両方を検索し、タイトルに日付が含まれるページを優先

### Markdown変換
- テーブルはcode blockとして変換（Notion tableへの完全変換は未対応）
- インラインのbold/italicは現在非対応（plain textとして扱う）

---

## 参考

- [Notion API Documentation](https://developers.notion.com/)
- [Notion API - Create a page](https://developers.notion.com/reference/post-page)
- [Notion API - Append block children](https://developers.notion.com/reference/patch-block-children)
