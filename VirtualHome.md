# VirtualHome ActionScript の実行方法

このリポジトリには、VirtualHome 形式の ActionScript を AI2-THOR の iTHOR シーンで実行するスクリプトがあります。

## 準備

依存パッケージをインストールした `$HOME/venv` の Python を使います。まだ環境を作っていない場合は、リポジトリのルートで次のように準備します。

```bash
python3 -m venv "$HOME/venv"
"$HOME/venv/bin/pip" install -r requirements.txt
"$HOME/venv/bin/pip" install -e .
```

シミュレータを画面に表示して実行するには、利用可能なディスプレーが必要です。画面なしで実行する場合は `--headless` を指定してください。

ProcTHOR-10K の家を使う場合は、追加で `prior` をインストールします。

```bash
"$HOME/venv/bin/pip" install prior
```

## ActionScript ファイル

UTF-8 のテキストファイルに、1行につき1アクションを記述します。空行と `#` または `//` で始まるコメント行は無視されます。オブジェクト名とIDは `<名前> (ID)` の形式です。

```text
# 本を拾い、テーブルランプの近くまで運ぶ例
<char0> [WALK] <book> (0)
<char0> [GRAB] <book> (0)
<char0> [WALK] <tablelamp> (76)
```

同梱の [`scripts/virtualhome_book_carry.txt`](scripts/virtualhome_book_carry.txt) も実行例として使えます。

## 基本実行

リポジトリのルートから実行します。

```bash
"$HOME/venv/bin/python" scripts/run_virtualhome_script.py \
  scripts/virtualhome_book_carry.txt \
  --scene FloorPlan1
```

`--scene` は iTHOR のシーン名です。省略時は `FloorPlan1` になり、次の4カテゴリから選べます（各30シーン、合計120シーン）。番号だけの名前を指定すると、内部で `_physics` が補われます。

| カテゴリ | シーン名 |
| --- | --- |
| キッチン | `FloorPlan1`〜`FloorPlan30` |
| リビング | `FloorPlan201`〜`FloorPlan230` |
| ベッドルーム | `FloorPlan301`〜`FloorPlan330` |
| バスルーム | `FloorPlan401`〜`FloorPlan430` |

ディスプレーを使わない場合は `--headless` を加えます。

## ProcTHOR-10K の家を使う

`--procthor-split` と `--procthor-index` で、データセットから読み込む家を指定できます。インデックスは0始まりで、省略時は `0` です。初回は `prior` がデータセットを取得するため、ネットワーク接続が必要になる場合があります。

```bash
"$HOME/venv/bin/python" scripts/run_virtualhome_script.py \
  scripts/virtualhome_book_carry.txt \
  --procthor-split train --procthor-index 0 \
  --video-out output/procthor-book-carry.mp4 \
  --camera-view follow \
  --width 1024 --height 768 --video-fps 66.666
```

`--procthor-split` は `train`、`val`、`test` から選びます。家を明示しない場合は従来どおり `FloorPlan1` が使われます。`--scene` と `--procthor-split` は同時に指定できません。

現在の既定シミュレータビルドは AI2-THOR 5.0.0 で、ProcTHOR-10K の現行データが要求する 5.0 以降に対応しています。

## 動画を録画する

`--video-out` を指定すると MP4 を保存します。動画録画時はヘッドレスにせず、シミュレータが画面を描画できる状態で実行してください。

```bash
"$HOME/venv/bin/python" scripts/run_virtualhome_script.py \
  scripts/virtualhome_book_carry.txt \
  --scene FloorPlan1 \
  --video-out output/book-carry.mp4 \
  --camera-view follow \
  --width 1024 --height 768 \
  --video-fps 66.666
```

- `--camera-view agent` はロボット視点、`follow` は追従カメラです。既定値は `agent` です。
- `--show-camera` を追加すると、録画対象の映像を別ウィンドウにライブ表示します。X11 ディスプレーが必要です。
- `--width` と `--height` はシミュレータ画像の幅・高さ（既定値 `1024` と `768`）です。
- `--video-fps` は出力動画のフレームレート（既定値 `66.666`）です。15 ms/frame に相当する値を指定する例は `66.666` です。
- 追従カメラの相対位置は `--camera-position RIGHT,UP,BEHIND` でメートル単位に変更できます。既定値は `-0.45,0.25,0.45` です。対象を保持中は対象物を基準に追従します。

## ID と変数

VirtualHome の数値 ID は AI2-THOR の `objectId` と一致するとは限りません。ID `0` は未解決 ID として扱い、ActionScript のオブジェクト名（ObjectType）から対象を探します。同じ種類の対象が複数あるときは、エージェントに水平距離が最も近いものを選びます。

対応を明示したい場合は `--object-map` を使います。複数の対応を指定できます。

```bash
"$HOME/venv/bin/python" scripts/run_virtualhome_script.py task.txt \
  --object-map '365=Book|1' \
  --object-map '76=TableLamp|1'
```

スクリプト中では `$BOOK_ID` のような変数も使えます。`--variable` で値を渡します。

```text
<char0> [GRAB] <book> ($BOOK_ID)
```

```bash
"$HOME/venv/bin/python" scripts/run_virtualhome_script.py task.txt \
  --variable BOOK_ID=0
```

## 実行結果とエラー

各アクションの成功・失敗は標準出力／標準エラーに行番号付きで表示されます。`--json-out results.json` を指定すると、各行の元アクションと結果を JSON に保存します。既定では最初の失敗で停止します。失敗後も続ける場合は `--continue-on-error` を指定してください。

実装されているアクションは `WALK`、`GRAB`、`OPEN`、`CLOSE`、`PUTIN`、`FIND`、`TURNTO`、`LOOKAT_LONG`、`TOUCH`、`PLUGIN` です。VirtualHome の全アクションを実装しているわけではなく、AI2-THOR に同等の状態がない操作は近似動作またはエラーになります。

コマンドの全オプションは次で確認できます。

```bash
"$HOME/venv/bin/python" scripts/run_virtualhome_script.py --help
```
