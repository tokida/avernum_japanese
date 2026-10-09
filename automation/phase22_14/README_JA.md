# Phase 22.14: 隔離起動とタイトル画面キャプチャ

このツールは、既存Phase22.13 pilotを`--detect-only`で起動し、Avernumウィンドウが一意に確認でき、かつ現在アクティブな場合だけSpectacleでウィンドウ単体PNGを最大3枚取得します。タイトル画面の画像解析やContinue／Load等の操作は行いません。スクリーンショット、pilotログ、結果ZIPの控えはGit作業ツリー外のユーザーキャッシュに保存します。公開GitHubや`.relaydeck`へ画像・セーブ・ゲーム資産を追加しないでください。

既定は副作用のないdry-runです。

```sh
python3 automation/phase22_14/capture_boot.py
```

次の一回を通常のKDE Wayland端末から実行すると、隔離pilotを75秒まで監視し、起動後2秒・8秒・20秒を目安に撮影します。

```sh
python3 automation/phase22_14/capture_boot.py --run --activate-game
```

`--activate-game`を省くと、ゲームが既にアクティブな場合だけ撮影します。付けた場合でも、タイトル検索が一意で、前後のアクティブウィンドウIDが一致した場合だけ対象Avernumを前面化します。KWinのIDは不透明な値として比較し、レポートに書きません。別のAvernumプロセス／ウィンドウ、D-Bus/KWin照会失敗、SpectacleのCLI差異、曖昧なタイトル、フォーカス不一致は起動または撮影をfail-closedで中止します。

実行物は`~/.cache/avernum-jp-smoke/phase22_14/run_<時刻>_<ランダム値>/`に保存し、ディレクトリは所有者だけがアクセスできるモードで作成します。レポートJSONは状態・終了コード・撮影成否だけを持ち、ユーザー名、絶対パス、ウィンドウID／タイトル、環境変数値を含みません。pilotの生ログとPNGは同じ非公開ディレクトリ内にあり、外部共有しないでください。既存のPhase22.13結果ZIPがあれば、起動前に同じ非公開ディレクトリへcopy2し、SHA-256とZIP整合性を確認します。終了後の新しいZIPも別名で控えます。

## 安全性と限界

- 実起動は`--run`だけです。`--dry-run`はGUIコマンドを実行せず、起動・撮影・フォーカス変更もしません。
- 起動時に既存ゲームプロセス／ウィンドウがあれば中止します。`--detect-only`のクリック・フォーカス変更が既存ソース内で明示的なclick-choice分岐に守られていることをASTで確認してから起動します。ソースが変わり検査できない場合は中止します。
- pilotの`--detect-only`はクリックを抑止しますが、既存pilotは終了要求が既定で有効です。Andrew検出時やタイムアウト時に、検証済みのAvernumウィンドウへ通常の終了要求を出します。強制killは行いません。
- `spectacle --activewindow --background --nonotify --output ...`を使います。撮影直前・直後に一意なゲームウィンドウとアクティブIDを再確認し、不一致なら画像を成功扱いせず一時画像を破棄します。`--activate-game`以外で前面化しません。デスクトップ上でユーザーが同時にウィンドウを切り替える競合は完全には排除できないため、前後照合も必須です。
- Spectacleのオプションは実行前に`--help`で照合します。現行ホストのKWin ID形式、Waylandポータルの可否、Spectacleが実際にPNGを保存できるかは環境依存で未検証です。確認に失敗したらゲームを起動しません。
- Phase22.13は専用の新しいProton prefixを作るため、既存prefixのセーブは自動共有されません。セーブの有無・ロード経路は別途明示確認が必要です。画面内のタイトル／Load操作位置を推測して押す機能はありません。
- 結果ZIPはPhase22.13側の固定名出力です。既存ZIPを上書きする前に検証済みバックアップを取り、出力を非公開キャッシュにコピーします。バックアップ不能なら起動しません。

テストは標準ライブラリだけで実行します。

```sh
python3 -m unittest discover -s automation/phase22_14/tests -v
```

## Phase 22.14.1: 起動設定ダイアログの読取専用検出

添付された474x602画像を基準に、Avernumタイトルの単一ウィンドウについてKWin UUID、正確なタイトル、アクティブウィンドウ一致、geometryをkdotoolの読取専用コマンドで照合し、候補操作点を出力します。既定の`--dry-run`と`--plan`は同じ動作です。

```sh
python3 automation/phase22_14/startup_dialog.py --dry-run
python3 -m unittest discover -s automation/phase22_14/tests -v
```

候補点は画面全体の固定座標ではなく、ダイアログ外枠左上を原点にした相対値です。参照外枠はleft=35/top=22/width=400/height=554、内部候補は1024x768=(145,161)、Play Window=(104,441)、Always Start=(104,467)、OK=(347,529)です。KWin geometryの幅・高さが400x554から各8ピクセルを超えて異なる場合はblockedになります。スクリーンショットの1枚から推定した値であり、DPIスケーリングや描画位置を画像認識で照合していません。

設定項目の選択状態はOCR等で確認できず、常に`settings_content_verified=false`、`status=blocked`です。単一ウィンドウ、UUID形式、正確なタイトル、アクティブID一致、geometry一致が揃っても、内容の証拠がないため操作計画は未承認です。このモジュールにクリック・キー入力・フォーカス変更を実行する機能はありません。候補点を実操作に使う前に、画面上で項目状態と座標を別途確認する必要があります。

## Phase 22.14.2: テスト専用の永続Proton prefix

`persistent_test_prefix.py --prepare`は配布元マニフェストと固定EXE/DLL SHAガードを検証し、パッケージを`~/.cache/avernum-jp-smoke/phase22_14/persistent_pilot/`へ私有コピーします。コピー内の`base/run_proton_isolated.sh`だけに、`AVERNUM_JP_TEST_COMPAT_PATH`が指定されている場合はその専用prefixを使う1行パッチを適用します。元の配布元、Steamのゲーム本体、Steam純正prefixは変更しません。

```sh
python3 automation/phase22_14/persistent_test_prefix.py --prepare
python3 automation/phase22_14/persistent_test_prefix.py --status
```

`--run`は明示した場合だけ、準備済みコピーから`run_auto.sh --detect-only --timeout 90`を起動し、`~/.cache/avernum-jp-smoke/phase22_14/persistent_compatdata/`をProton prefixとして渡します。実行は通常のKDE Wayland端末で行ってください。GUIサンドボックスからは実行しません。クリックはdetect-only静的ガードで拒否し、別アプリへのフォーカス変更は行いません。Phase22.13既定動作により、終了時は検証したAvernum窓へ閉じる要求を行う場合があります。

初回は起動時の「Avernum Screen Size」画面が表示され、設定保存のため一度手作業が必要な可能性があります。このツールはその画面を操作せず、設定保存の有効性も未検証です。セーブは新しい専用prefix内だけに作られ、Steam側セーブのコピーや上書きはしません。既存のPhase22.13結果ZIPは起動前にprivate領域へcopy2し、SHA-256とZIP整合性を確認します。バックアップに失敗した場合は起動しません。実行後の結果ZIPもprivate領域へ控えます。

```sh
python3 automation/phase22_14/persistent_test_prefix.py --run
python3 -m unittest discover -s automation/phase22_14/tests -v
```
