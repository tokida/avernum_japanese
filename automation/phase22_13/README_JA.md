# Phase 22.13 KDE/Wayland preflight（安全試作）

この診断はKDE/Wayland上でGUI自動テストを準備するため、セッション種別・デスクトップ名・`python3`/`git`/`kdotool`/`ydotool`の有無・Wayland表示変数の有無をJSONに記録します。ゲームやGUIを起動せず、クリック・キー入力・sudo・インストール・認証情報の取得を行いません。ユーザー名、HOME、秘密値、フルパス、プロセスコマンドラインは記録しません。

```sh
python3 automation/phase22_13/preflight.py
python3 automation/phase22_13/preflight.py --output /tmp/phase22_13_preflight.json
python3 -m unittest discover -s automation/phase22_13/tests -v
```

既定の出力先は `.relaydeck/phase22_13_preflight.json` です。`--output`では指定先の親ディレクトリも作成し、JSONを書き込みます。

## 次の段階

1. `kdotool` または `ydotool` が必要になった場合は、導入前にユーザーの承認を得てください。この試作はインストールしません。
2. まず必要なGUIテスト対象・手順・安全条件をレビューします。
3. 実機でのゲーム起動やGUI入力テストは後日、ユーザーの明示的な指示を得てから実施します。
4. ゲーム本体、DLL、セーブデータはGitに追加しません。

## 実機テストrunner（Phase 22.13）

```sh
python3 automation/phase22_13/runner.py --dry-run
python3 -m unittest discover -s automation/phase22_13/tests -v
```

`runner.py`はdry-runを既定にし、候補資産の存在、GUI/Protonツール、操作計画、検証不能項目を `.relaydeck/phase22_13_dry_run.json` に出力します。ユーザー名、HOME、秘密値、絶対パス、プロセスコマンドラインは記録しません。ゲーム起動とGUI入力は行いません。`--run`は`--timeout`が必須ですが、この試作には安全な起動/入力アダプターがないため、指定してもブロックされます。

クリック証拠は同一run/frame/window/choice、最新イベント番号、現時点のフォーカス、座標、および2秒以内の記録をすべて要求します。古いログの座標は許可しません。日本語表示とクリック結果は証拠が得られるまで「未検証」です。ゲーム本体、DLL、セーブはGitへ追加しないでください。

## ホストKDE診断

通常のKDE Waylandターミナルで次の1コマンドを実行すると、環境の有無と接続前提だけをJSONに記録します。

```sh
python3 automation/phase22_13/kde_host_probe.py
```

出力先は `.relaydeck/phase22_13_kde_host_probe.json` です。環境変数の値、ユーザー名、ホーム、ソケットパス、ウィンドウID/タイトル、認証値は記録・表示しません。`kdotool getactivewindow` の標準出力と標準エラーも保存しません。GUI入力は一切試さず、`actual_input_status` は常に `unknown` です。

この結果はスクリプトを実際に実行した環境だけを評価します。RelayDeckや別のサンドボックスで実行した結果はホストKDEセッションの確認にならないため、必ずホストKDEターミナルで再実行してください。
