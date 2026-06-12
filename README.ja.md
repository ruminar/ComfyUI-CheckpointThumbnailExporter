# ComfyUI-CheckpointThumbnailExporter (README.ja.md)

`Checkpoint Thumbnail Exporter` は、`HandpickerSuite` で生成された「おぬしの戦利品（チェックポイント名付きの画像フォルダ）」を逆引きスキャンし、`OGN-ModelManager` 用のチェックポイントサムネイルを**人間の手作業1ミリもなしに全自動で配置する**（←若干誇張あり）、単体のユーティリティパネルじゃ！

あの「NO PREVIEW（灰色の墓標の山）」と化したモデル選択画面に、おぬしの環境で実際に生み出された至高の美少女たちの画像を、最速かつ全自動でブチ込むために建造されたのじゃ！
（注：civitaiからサムネイルをダウンロードする機能はありません）

---

## 🥝 コア・ストーリー（兵兵リレー）

```text
HandpickerSuiteで夜な夜な大量の低解像度ガチャを回す
↓
チェックポイント名が刻印された画像フォルダ（戦利品）が床下に蓄積される
↓
本ノードを起動：各モデルの「最新の生成画像」を一撃で自動ハント
↓
OGN-ModelManager互換のサムネイルが、モデルの横に全自動でインストール完了！
```

---

## 🛠️ 主な機能と「えげつない」こだわり

* **ターゲットファーストの超最適化設計**  
起動すると、まず最初にチェックポイントの横にある既存のサムネイルをチェックする。「すべてのモデルにすでにサムネイルがある場合、ソースフォルダのスキャンは行わずに終了する」という、おぬしのSSDとCPUに優しい、現場主義の爆速仕様じゃ！

* **GraphicsMagick不要・Pillow単体駆動の軽快さ**  
「サムネイルを作るだけならGM（重工業）なんていらん、Pillowだけで十分じゃ！」という割り切り設計。RGB変換、EXIFの回転補正（縦横補正）、JPEGコメントの埋め込みを、ComfyUI環境で通常利用可能な画像処理ライブラリだけでサラリとこなすぞ。

* **密結合の拒絶**  
本ノードは `OGN-ModelManager` の内部キャッシュやプライベートAPI、データベースには一切触れん。ただ「モデルの横に、同名の縮小 `.jpg` をそっと置く」という、OSレイヤーでの普遍的なファイル配置の責務のみ担当する。外部アプリに優しい仕様じゃ！

---

## 📦 インストール方法

ComfyUIの `custom_nodes` ディレクトリに、このフォルダをそのまま手動クローン（または展開）し、ComfyUIを再起動するが良い。

```text
ComfyUI/custom_nodes/ComfyUI-CheckpointThumbnailExporter
```

* **ノード名:** `Checkpoint Thumbnail Exporter`
* **カテゴリー:** `utils/checkpoint`

※本ノードは独立したユーティリティパネルじゃ。他のノードとワイヤーで配線する必要は一切ないぞ！

---

## 🏯 各設定項目（ウィジェット）の解説

```text
source_image_root
target_format
max_size
jpeg_quality
operation
run_mode
```

* **`source_image_root` (文字列)**  
`HandpickerSuite` や `GM Image Saver` などが画像を吐き出しているルートフォルダを指定する。**空欄のままにしておくと、ComfyUI標準の `output` フォルダを自動で使用するぞ。**

> ⚠️ **トラブルシューティングの罠！**  
> もし「すべてのチェックポイントがunmatched（未マッチ）」になる場合は、まずここを疑うのじゃ。GM Image Saverで出力先を別ドライブ等に明示的に飛ばしている場合、空欄（標準output参照）だと画像が見つからずに空振るぞ。

* **`target_format`**  
`OGN-ModelManager` 固定じゃ。

* **`max_size` (整数 / 初期値 512)**  
生成するサムネイルの最大縦横ピクセル数じゃ（アスペクト比は完全維持・縮小のみ駆動）。

* **`jpeg_quality` (整数 / 初期値 90)**  
出力されるサムネイルJPEGの画質じゃ。

* **`operation`**
  * `install_missing`: サムネイルのないチェックポイントを探して自動配置する。
  * `uninstall_managed`: 本ノードが過去に生成したサムネイルだけを安全に削除する。

* **`run_mode`**
  * `dry_run`: ファイルを一切変更せず、ログ上で「何が起きるか」をシミュレーションする。
  * `execute`: 実際にファイルを書き込み / 削除する本番モードじゃ。

---

## 🎨 漢のワンボタン挙動（インターフェース）

ノードのド真ん中には、選択した `operation` と `run_mode` に応じて名前とアイコンがガガガと切り替わる、漢のワンボタンが配備されておる。

* `install_missing` + `dry_run` ➔ **🎨 [Dry Run] Find Missing Thumbnails**
* `install_missing` + `execute` ➔ **🎨 [Execute!] Install Missing Thumbnails**
* `uninstall_managed` + `dry_run` ➔ **❌ [Dry Run] Find Managed Thumbnails**
* `uninstall_managed` + `execute` ➔ **❌ [Execute!] Uninstall Managed Thumbnails**

> 🔐 **プロマネ直伝の安全弁（暴走防止）：**
> * `operation` を切り替えると、`run_mode` は自動的に安全な `dry_run` へ強制リセットされる。
> * `execute`（本番実行）が1回完了すると、`run_mode` は自動的に `dry_run` へ即座に引き戻される。50mロール紙を連続で誤爆印刷させないための、冷徹なセーフティじゃ！
> * 進捗バーの下にあるReportエリアのアイコンはさらに厳格で、**実際にファイルが1枚以上書き込まれた／削除された本番実行時のみ** `🎨 Install complete.` / `❌ Uninstall complete.` のアイコンが点灯する仕様じゃ。

---

## 🔍 賢すぎるソース画像マッチングの仕様

本ノードは、チェックポイントの名前（`ckpt_name_safe` 互換のキー）を自動生成し、ソースフォルダ内の画像に対して以下の優先度でケースインセンシティブ（大文字小文字無視）なレーダーを飛ばす：

1. **優先度 0:** 画像の親フォルダ名が、チェックポイント名と完全一致
2. **優先度 1:** 画像の親フォルダ名に、チェックポイント名が含まれている（部分一致）
3. **優先度 2:** 画像のファイル名（拡張子除く）に、チェックポイント名が含まれている
4. **優先度 3:** 画像の相対パス全体に、チェックポイント名が含まれている

> 💡 **曖昧さの排除ルール:**  
> もし、1枚の画像が複数のチェックポイントに対して「同じ優先度」でマッチしてしまった場合、システムは「誤判定の危険あり」とみなしてその画像を**あえて無視（スキップ）**する。  
> 条件をクリアした複数の候補画像が見つかった場合は、**「ファイルの更新日時（mtime）が最も新しいもの（最新の戦利品）」**が自動的に王座に君臨するぞ！

---

## ❌ 安全すぎるアンインストール（管理マーカーのケジメ）

「6GBのモデルが入った城を壊さず、自分のゴミだけを片付ける」という忍道の美学に基づき、`uninstall_managed` は驚異的な安全性を誇る。

本ノードが生成したサムネイルJPEGの腹の中（Metadata Comment）には、以下の独自管理マーカー（`managed=true`）が密閉されておる：

```text
Checkpoint Thumbnail Exporter
managed=true
comment_schema=cte_comment_v1
target=OGN-ModelManager
...
```

アンインストールを実行した際、このコメントが検出されなかった画像（ユーザーが手動で置いたお気に入り画像など）は、削除対象にしない設計じゃ。

さらに、`uninstall_managed + execute` を実行するには、「事前に同じ設定で dry_run を行う」ことが必須仕様となっておる。確認なしの誤クリックで城内の画像が虚空へ消えることを極力回避する作りじゃ！

---

## ⚠️ 現在の対応範囲について

このノードは、まず次の用途に絞っています。

- OGN-ModelManager用の不足サムネイルを探す
- HandpickerSuite / GM Image Saverなどで生成された画像を使う
- Checkpointファイル横に同名の .jpg サムネイルを作成する
- 既存サムネイルは上書きしない

外部サービスからのサムネイル取得や、OGN-ModelManager内部状態の変更は行いません。
