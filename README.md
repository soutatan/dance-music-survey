# ダンスジャンル楽曲評価サイト

## 概要

- 回答者ごとに40曲を提示
- 5候補グループから各8曲を層化抽出
- これまでの割当回数が少ない曲を優先
- 音源は回答者ごとにランダム順
- Break / Pop / Lock / House / HipHop は複数選択可
- 「どのジャンルにも当てはまらない」「判断できない」も選択可
- 回答は1曲ごとにSQLiteへ保存
- 候補グループは回答者に表示しない

## 1. ファイル配置

このフォルダを以下の構成にしてください。

```text
dance_survey_streamlit/
├─ app.py
├─ requirements.txt
├─ audio/
│  ├─ Q0001.mp3
│  ├─ Q0002.mp3
│  └─ ...
├─ data/
│  └─ clip_mapping.csv
└─ .streamlit/
   ├─ config.toml
   └─ secrets.toml
```

### 音源

`D:\MTG_Jamendo_clips\survey_20s_mp3`のMP3を、`audio`フォルダへコピーします。

Windowsのコマンドプロンプト例：

```bat
xcopy "D:\MTG_Jamendo_clips\survey_20s_mp3\*.mp3" "C:\Users\宮下　颯汰\mtg-jamendo-dataset\dance_survey_streamlit\audio\" /Y
```

### 対応表

`D:\MTG_Jamendo_clips\clip_mapping.csv`を`data`フォルダへコピーします。

```bat
copy "D:\MTG_Jamendo_clips\clip_mapping.csv" "C:\Users\宮下　颯汰\mtg-jamendo-dataset\dance_survey_streamlit\data\clip_mapping.csv"
```

## 2. Streamlitのインストール

```bat
"C:\Anaconda3\python.exe" -m pip install -r requirements.txt
```

## 3. 管理者パスワード

`.streamlit/secrets.toml`を開き、パスワードを変更します。

```toml
ADMIN_PASSWORD = "change-this-password"
```

## 4. ローカル起動

```bat
"C:\Anaconda3\python.exe" -m streamlit run app.py
```

起動後、通常はブラウザで以下が開きます。

```text
http://localhost:8501
```

## 5. 動作確認

1. ニックネームを入力
2. 同意欄にチェック
3. 音源を再生
4. ジャンルまたは特別回答を選択
5. 次の曲へ進む
6. 管理者欄からCSVをダウンロード

## 重要

この初期版はローカル実行向けです。回答は`data/responses.db`へ保存されます。
Streamlit Community Cloudへ公開する場合、ローカルSQLiteは永続保存に適さないため、次の段階でSupabaseなどへ変更してください。
