<!--
Role: OCR全文をローカルJSONLに保存する前提で、除外（記録しない）とマスキング（置換して保存）の運用ルールを定義する。
How: まず安全側のキーワード/アプリ除外を決め、必要に応じて `EVERYTIME-LOG/config.json` に反映して運用で調整する（互換: `EVERLOG-LOG/config.json`）。
Key sections: 除外アプリ、除外ドメイン、マスキング、JSONL反映。
Collaboration: 実装は `everlog/exclusions.py` と `everlog/redact.py`、設定値は `everlog/config.py` が扱う。
-->
# everlog 除外・マスキングルール v0.1

目的: **OCR全文を保存する方針**のため、機微情報が残りやすい画面は「記録しない」または「マスキングして記録する」。

## 1. 基本方針
- **除外（hard exclude）**: スクショもOCRも行わず、JSONLには「除外イベント（スタブ）」だけを残す
  - 例: `excluded=true`, `excluded_reason="app:1Password"` のように残し、`ocr_text` は空/欠落にする
- **マスキング（redact）**: OCR後にテキストを正規表現で置換してからJSONLに保存する
- 迷う場合は安全側（除外）に倒す

## 2. 除外アプリ（hard exclude）
次のアプリが前面の場合は除外する。
- `1Password`

（運用で増やす想定: パスワードマネージャ、キーチェーン、認証アプリ、銀行アプリ など）

## 3. 除外ドメイン（hard exclude）
「銀行系 / ログイン系 / 決済情報っぽい画面系」を対象に、次のいずれかに当てはまる場合は除外する。

### 3.1 ドメインベース（Chrome URL取得ができた場合）
ドメイン（またはURL文字列）に以下のキーワードが含まれる場合は除外。
- 銀行系: `bank`, `netbank`, `onlinebank`, `atm`, `creditunion`
- ログイン系: `login`, `signin`, `auth`, `sso`, `oauth`, `account`
- 決済系: `pay`, `payment`, `checkout`, `billing`, `invoice`, `stripe`, `paypal`

※ 実際の運用では「自分が使うサービスのドメインを明示的に列挙」するのが最も確実。

### 3.2 画面テキスト/タイトルベース（URLが取れない場合のフォールバック）
ウィンドウタイトルやOCRテキストに以下のキーワードが含まれる場合は除外（または少なくとも強いマスキング）。
- 日本語: `ログイン`, `サインイン`, `パスワード`, `暗証番号`, `二段階`, `認証`, `カード番号`, `セキュリティコード`, `お支払い`, `請求`
- 英語: `Sign in`, `Login`, `Password`, `One-time`, `2FA`, `Verification`, `Card number`, `CVV`, `Security code`, `Billing`

## 4. マスキング（redact patterns）
以下は「除外しない」場合でも、OCRテキスト上で検出したらマスクする。

### 4.1 クレジットカードっぽい文字列
- 13〜19桁程度の連続数字（スペース/ハイフン区切りを含む）を検出して `[REDACTED_CARD]` に置換
- Luhnチェック（可能なら）で誤検出を減らす

### 4.2 パスワード/認証情報っぽい部分
- `password`, `passcode`, `OTP`, `verification code`, `secret` 等の近傍（同一行/前後行）を `[REDACTED_AUTH]` に置換
- `••••` のような伏字周辺も同様に扱う

### 4.3 メール/電話（任意）
- メール: `something@domain` を `[REDACTED_EMAIL]`
- 電話: 国番号/ハイフン区切りを含む電話番号っぽいものを `[REDACTED_PHONE]`

## 5. 出力（JSONL）への反映方針
- 除外イベント:
  - `excluded=true`
  - `excluded_reason` に `app:<name>` / `domain:<domain>` / `keyword:<kw>` を入れる
  - `ocr_text` は保存しない
  - `window_title` は `[REDACTED]` にする（安全側）
- 通常イベント:
  - `ocr_text` は保存するが、上記パターンでマスキング後の文字列を保存する

## 5.1 Notion同期への反映
- NotionにはOCR全文を同期しない前提のため、Notion側は「日次サマリ＋タイムライン（+必要なら短い抜粋）」のみ扱う
- そのため、本ファイルの除外/マスキングは主に **ローカルJSONLに残す内容の安全性** のために適用する

## 6. 運用メモ
- 「除外」の判定は強めにしてOK（時間集計は `active_app` だけでも成立する）
- 重要なのは *一度JSONLに残ると消しにくい* 点。迷ったら除外。
