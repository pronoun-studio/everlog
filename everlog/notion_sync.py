# Role: Notionデータベースへの日報同期を行う。
# How: Notion APIを使用してマークダウン日報をNotionページとして作成/更新する。
# Key functions: `sync_daily()`, `retry_pending()`, `mark_pending()`
# Collaboration: `everlog/summarize.py` から呼び出される。
from __future__ import annotations

import json
import os
import re
import time
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import load_config
from .safety import sanitize_text_for_sharing

class NotionSyncError(Exception):
    """Notion同期エラー"""
    pass


def _get_pending_path() -> Path:
    """pending.json のパスを返す"""
    everlog_dir = Path.home() / ".everlog"
    everlog_dir.mkdir(parents=True, exist_ok=True)
    return everlog_dir / "notion_pending.json"


def _load_pending() -> dict[str, Any]:
    """pending.json を読み込む"""
    path = _get_pending_path()
    if not path.exists():
        return {"pending": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"pending": []}
        if "pending" not in data:
            data["pending"] = []
        return data
    except Exception:
        return {"pending": []}


def _save_pending(data: dict[str, Any]) -> None:
    """pending.json を保存する"""
    path = _get_pending_path()
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def mark_pending(
    date: str,
    run_id: str,
    md_path: Path,
    daily_llm_path: Path,
    error_msg: str,
) -> None:
    """同期失敗を pending.json に記録する"""
    data = _load_pending()
    
    # 同じ date の既存エントリを削除（最新のrun_idのみ保持）
    data["pending"] = [p for p in data["pending"] if p.get("date") != date]
    
    data["pending"].append({
        "date": date,
        "run_id": run_id,
        "md_path": str(md_path),
        "daily_llm_path": str(daily_llm_path),
        "failed_at": datetime.now().astimezone().isoformat(),
        "retry_count": 0,
        "last_error": error_msg,
    })
    _save_pending(data)
    print(f"[notion_sync] Marked as pending: {date} ({error_msg})")


def remove_pending(date: str) -> None:
    """同期成功時に pending から削除する"""
    data = _load_pending()
    before = len(data["pending"])
    data["pending"] = [p for p in data["pending"] if p.get("date") != date]
    if len(data["pending"]) < before:
        _save_pending(data)


def _get_api_key() -> str:
    """環境変数から Notion API Key を取得"""
    key = os.environ.get("NOTION_API_KEY", "").strip()
    if not key:
        raise NotionSyncError("NOTION_API_KEY is not set")
    return key


def _get_database_id() -> str:
    """環境変数から Database ID を取得"""
    db_id = os.environ.get("NOTION_DATABASE_ID", "").strip()
    if not db_id:
        raise NotionSyncError("NOTION_DATABASE_ID is not set")
    # ハイフンを削除（32文字形式に統一）
    return db_id.replace("-", "")


def _notion_request(
    method: str,
    endpoint: str,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Notion API にリクエストを送る"""
    api_key = _get_api_key()
    url = f"https://api.notion.com/v1{endpoint}"
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28",
    }
    
    data = json.dumps(body).encode("utf-8") if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8") if e.fp else ""
        raise NotionSyncError(f"Notion API error {e.code}: {error_body}")
    except urllib.error.URLError as e:
        raise NotionSyncError(f"Network error: {e.reason}")


def _find_page_by_date(database_id: str, date: str) -> dict[str, Any] | None:
    """日付でページを検索（重複チェック用）
    
    ジャンルが「AutoLog」のページのみを対象とし、
    タイムゾーンの問題を回避するため日付範囲でフィルタリングする。
    """
    # NotionはUTC基準で日付を保存するため、JSTの日付は+1日にずれることがある
    # そのため、date と date+1日 の両方を含む範囲でフィルタリングする
    from datetime import datetime, timedelta
    try:
        dt = datetime.strptime(date, "%Y-%m-%d")
        next_day = (dt + timedelta(days=1)).strftime("%Y-%m-%d")
    except ValueError:
        next_day = date
    
    body = {
        "filter": {
            "and": [
                {
                    "property": "ジャンル",
                    "multi_select": {
                        "contains": "AutoLog",
                    },
                },
                {
                    "or": [
                        {
                            "property": "実施日_編集可",
                            "date": {
                                "equals": date,
                            },
                        },
                        {
                            "property": "実施日_編集可",
                            "date": {
                                "equals": next_day,
                            },
                        },
                    ]
                },
            ]
        },
        "page_size": 10,
        "sorts": [{"property": "作成日時", "direction": "descending"}],
    }
    try:
        result = _notion_request("POST", f"/databases/{database_id}/query", body)
        results = result.get("results") or []
        # タイトルに日付が含まれるページを優先
        short_date = date[2:]  # "2026-02-07" -> "26-02-07"
        for page in results:
            props = page.get("properties", {})
            title_prop = props.get("活動ログ", {}).get("title", [])
            title = title_prop[0]["plain_text"] if title_prop else ""
            if short_date in title or date in title:
                return page
        # 見つからなければ最初の結果を返す
        if results:
            return results[0]
        return None
    except NotionSyncError:
        return None


def _md_to_notion_blocks(md_content: str) -> list[dict[str, Any]]:
    """マークダウンをNotionブロックに変換する"""
    blocks: list[dict[str, Any]] = []
    lines = md_content.split("\n")
    
    i = 0
    while i < len(lines):
        line = lines[i]
        
        # 見出し
        if line.startswith("### "):
            blocks.append({
                "object": "block",
                "type": "heading_3",
                "heading_3": {
                    "rich_text": [{"type": "text", "text": {"content": line[4:].strip()}}]
                }
            })
            i += 1
            continue
        
        if line.startswith("## "):
            blocks.append({
                "object": "block",
                "type": "heading_2",
                "heading_2": {
                    "rich_text": [{"type": "text", "text": {"content": line[3:].strip()}}]
                }
            })
            i += 1
            continue
        
        if line.startswith("# "):
            blocks.append({
                "object": "block",
                "type": "heading_1",
                "heading_1": {
                    "rich_text": [{"type": "text", "text": {"content": line[2:].strip()}}]
                }
            })
            i += 1
            continue
        
        # テーブル（簡易対応: |で始まる行をコードブロックとして扱う）
        if line.startswith("|"):
            table_lines = []
            while i < len(lines) and lines[i].startswith("|"):
                table_lines.append(lines[i])
                i += 1
            # テーブルをコードブロックとして追加
            table_content = "\n".join(table_lines)
            if len(table_content) > 2000:
                table_content = table_content[:1997] + "..."
            blocks.append({
                "object": "block",
                "type": "code",
                "code": {
                    "rich_text": [{"type": "text", "text": {"content": table_content}}],
                    "language": "plain text"
                }
            })
            continue
        
        # 箇条書きリスト
        if line.startswith("- "):
            content = line[2:].strip()
            if len(content) > 2000:
                content = content[:1997] + "..."
            blocks.append({
                "object": "block",
                "type": "bulleted_list_item",
                "bulleted_list_item": {
                    "rich_text": [{"type": "text", "text": {"content": content}}]
                }
            })
            i += 1
            continue
        
        # 番号付きリスト
        if re.match(r"^\d+\.\s", line):
            content = re.sub(r"^\d+\.\s", "", line).strip()
            if len(content) > 2000:
                content = content[:1997] + "..."
            blocks.append({
                "object": "block",
                "type": "numbered_list_item",
                "numbered_list_item": {
                    "rich_text": [{"type": "text", "text": {"content": content}}]
                }
            })
            i += 1
            continue
        
        # 空行
        if not line.strip():
            i += 1
            continue
        
        # 通常の段落
        content = line.strip()
        if content:
            if len(content) > 2000:
                content = content[:1997] + "..."
            blocks.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"type": "text", "text": {"content": content}}]
                }
            })
        i += 1
    
    return blocks


def _create_page(
    database_id: str,
    title: str,
    date: str,
    blocks: list[dict[str, Any]],
) -> dict[str, Any]:
    """新規ページを作成する"""
    # Notion APIは一度に100ブロックまで
    blocks_to_create = blocks[:100]
    
    body = {
        "parent": {"database_id": database_id},
        "properties": {
            "活動ログ": {
                "title": [{"type": "text", "text": {"content": title}}]
            },
            "実施日_編集可": {
                "date": {"start": date}
            },
            "ジャンル": {
                "multi_select": [{"name": "AutoLog"}]
            },
        },
        "children": blocks_to_create,
    }
    
    result = _notion_request("POST", "/pages", body)
    
    # 100ブロック以上の場合は追加でappend
    if len(blocks) > 100:
        page_id = result["id"]
        remaining = blocks[100:]
        for i in range(0, len(remaining), 100):
            chunk = remaining[i:i+100]
            _notion_request("PATCH", f"/blocks/{page_id}/children", {"children": chunk})
    
    return result


def _update_page(
    page_id: str,
    title: str,
    date: str,
    blocks: list[dict[str, Any]],
) -> dict[str, Any]:
    """既存ページを更新する"""
    # プロパティを更新
    body = {
        "properties": {
            "活動ログ": {
                "title": [{"type": "text", "text": {"content": title}}]
            },
            "実施日_編集可": {
                "date": {"start": date}
            },
            "ジャンル": {
                "multi_select": [{"name": "AutoLog"}]
            },
        },
    }
    result = _notion_request("PATCH", f"/pages/{page_id}", body)
    
    # 既存のブロックを削除
    try:
        children_resp = _notion_request("GET", f"/blocks/{page_id}/children?page_size=100")
        for child in children_resp.get("results") or []:
            child_id = child.get("id")
            if child_id:
                try:
                    _notion_request("DELETE", f"/blocks/{child_id}")
                except NotionSyncError:
                    pass  # 削除失敗は無視
    except NotionSyncError:
        pass  # 取得失敗は無視
    
    # 新しいブロックを追加
    for i in range(0, len(blocks), 100):
        chunk = blocks[i:i+100]
        _notion_request("PATCH", f"/blocks/{page_id}/children", {"children": chunk})
    
    return result


def _fix_date_after_automation(page_id: str, date: str, wait_seconds: int = 60) -> None:
    """Notionオートメーション後に実施日_編集可を正しい日付に再設定する
    
    Notion側で「作成日 → 実施日_編集可」のオートメーションが走るため、
    ページ作成/更新後に少し待機してから日付を再設定する。
    
    Args:
        page_id: NotionページID
        date: 正しい日付（YYYY-MM-DD形式）
        wait_seconds: オートメーションを待機する秒数（デフォルト60秒）
    """
    print(f"[notion_sync] Waiting {wait_seconds}s for Notion automation...")
    time.sleep(wait_seconds)
    
    body = {
        "properties": {
            "実施日_編集可": {
                "date": {"start": date}
            },
        },
    }
    _notion_request("PATCH", f"/pages/{page_id}", body)
    print(f"[notion_sync] Fixed date to {date}")


def sync_daily(
    date: str,
    run_id: str,
    md_path: Path,
    daily_llm_path: Path,
) -> bool:
    """日報をNotionに同期する
    
    Returns:
        True: 同期成功
        False: 同期失敗（pendingに記録済み）
    """
    try:
        database_id = _get_database_id()
        
        # マークダウンを読み込み
        if not md_path.exists():
            raise NotionSyncError(f"Markdown file not found: {md_path}")
        md_content = md_path.read_text(encoding="utf-8")
        
        # daily_llm.json からタイトルを取得
        title = f"作業ログ {date}"
        if daily_llm_path.exists():
            try:
                cfg = load_config()
                safe_md_raw = str(
                    os.environ.get("EVERLOG_SAFE_MARKDOWN")
                    or os.environ.get("EVERYTIMECAPTURE_SAFE_MARKDOWN")
                    or ""
                ).strip()
                safe_md_enabled = safe_md_raw not in {"0", "false", "FALSE", "no", "NO"}
                daily_data = json.loads(daily_llm_path.read_text(encoding="utf-8"))
                daily = daily_data.get("daily") if isinstance(daily_data, dict) else None
                if isinstance(daily, dict):
                    t = str(daily.get("daily_title") or "").strip()
                    if t:
                        title = (
                            " ".join(sanitize_text_for_sharing(t, cfg).split())
                            if safe_md_enabled
                            else t
                        )
            except Exception:
                pass
        
        # Notionブロックに変換
        blocks = _md_to_notion_blocks(md_content)
        
        # 既存ページを検索
        existing = _find_page_by_date(database_id, date)
        
        if existing:
            # 更新
            page_id = existing["id"]
            _update_page(page_id, title, date, blocks)
            print(f"[notion_sync] Updated existing page: {date}")
            # オートメーション後に日付を再設定
            _fix_date_after_automation(page_id, date)
        else:
            # 新規作成
            result = _create_page(database_id, title, date, blocks)
            print(f"[notion_sync] Created new page: {date}")
            # オートメーション後に日付を再設定
            _fix_date_after_automation(result["id"], date)
        
        # 成功したらpendingから削除
        remove_pending(date)
        return True
        
    except NotionSyncError as e:
        mark_pending(date, run_id, md_path, daily_llm_path, str(e))
        return False
    except Exception as e:
        mark_pending(date, run_id, md_path, daily_llm_path, f"Unexpected error: {e}")
        return False


def retry_pending() -> int:
    """未同期のエントリを再試行する
    
    Returns:
        成功した件数
    """
    data = _load_pending()
    if not data["pending"]:
        return 0
    
    success_count = 0
    failed_items: list[dict[str, Any]] = []
    
    for item in data["pending"]:
        date = item.get("date", "")
        run_id = item.get("run_id", "")
        md_path = Path(item.get("md_path", ""))
        daily_llm_path = Path(item.get("daily_llm_path", ""))
        retry_count = int(item.get("retry_count", 0))
        
        if not date or not md_path.exists():
            # 無効なエントリはスキップ（削除）
            continue
        
        try:
            database_id = _get_database_id()
            md_content = md_path.read_text(encoding="utf-8")
            
            title = f"作業ログ {date}"
            if daily_llm_path.exists():
                try:
                    cfg = load_config()
                    safe_md_raw = str(
                        os.environ.get("EVERLOG_SAFE_MARKDOWN")
                        or os.environ.get("EVERYTIMECAPTURE_SAFE_MARKDOWN")
                        or ""
                    ).strip()
                    safe_md_enabled = safe_md_raw not in {"0", "false", "FALSE", "no", "NO"}
                    daily_data = json.loads(daily_llm_path.read_text(encoding="utf-8"))
                    daily = daily_data.get("daily") if isinstance(daily_data, dict) else None
                    if isinstance(daily, dict):
                        t = str(daily.get("daily_title") or "").strip()
                        if t:
                            title = (
                                " ".join(sanitize_text_for_sharing(t, cfg).split())
                                if safe_md_enabled
                                else t
                            )
                except Exception:
                    pass
            
            blocks = _md_to_notion_blocks(md_content)
            existing = _find_page_by_date(database_id, date)
            
            if existing:
                page_id = existing["id"]
                _update_page(page_id, title, date, blocks)
                print(f"[notion_sync] Retry success (update): {date}")
                # オートメーション後に日付を再設定
                _fix_date_after_automation(page_id, date)
            else:
                result = _create_page(database_id, title, date, blocks)
                print(f"[notion_sync] Retry success (create): {date}")
                # オートメーション後に日付を再設定
                _fix_date_after_automation(result["id"], date)
            
            success_count += 1
            
        except NotionSyncError as e:
            # リトライ失敗
            item["retry_count"] = retry_count + 1
            item["last_error"] = str(e)
            item["failed_at"] = datetime.now().astimezone().isoformat()
            failed_items.append(item)
            
            if retry_count + 1 >= 5:
                print(f"[notion_sync] Warning: {date} has failed {retry_count + 1} times. Manual intervention may be needed.")
            else:
                print(f"[notion_sync] Retry failed ({retry_count + 1}): {date} - {e}")
                
        except Exception as e:
            item["retry_count"] = retry_count + 1
            item["last_error"] = f"Unexpected: {e}"
            item["failed_at"] = datetime.now().astimezone().isoformat()
            failed_items.append(item)
            print(f"[notion_sync] Retry failed ({retry_count + 1}): {date} - {e}")
    
    # pending を更新
    data["pending"] = failed_items
    _save_pending(data)
    
    return success_count


def notion_sync_enabled() -> bool:
    """Notion同期が有効かどうかを確認"""
    enabled = str(os.environ.get("EVERLOG_NOTION_SYNC", "")).strip()
    return enabled in {"1", "true", "TRUE", "yes", "YES"}


def get_pending_count() -> int:
    """未同期の件数を取得"""
    data = _load_pending()
    return len(data.get("pending", []))
