import json
import random
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st
from supabase import create_client

APP_DIR = Path(__file__).parent
DATA_DIR = APP_DIR / "data"
MAPPING_CSV = DATA_DIR / "clip_mapping.csv"
CANDIDATES_CSV = DATA_DIR / "dance_candidates_revised.csv"

QUESTIONS_PER_RESPONDENT = 40
PER_GROUP = 8
GENRES = ["Break", "Pop", "Lock", "House", "HipHop"]
SPECIAL_OPTIONS = ["どのジャンルにも当てはまらない", "判断できない"]

st.set_page_config(
    page_title="ダンスジャンル楽曲評価",
    page_icon="🎧",
    layout="centered",
)


@st.cache_resource
def get_supabase():
    """Streamlit Secretsからサーバー専用Supabaseクライアントを作成する。"""
    required = ["SUPABASE_URL", "SUPABASE_SECRET_KEY", "SUPABASE_AUDIO_BASE_URL"]
    missing = [key for key in required if not st.secrets.get(key)]
    if missing:
        raise RuntimeError(f"Secretsに必要な設定がありません: {', '.join(missing)}")
    return create_client(
        str(st.secrets["SUPABASE_URL"]),
        str(st.secrets["SUPABASE_SECRET_KEY"]),
    )


def init_database():
    """公開版ではテーブルをSupabase SQL Editorで作成済みのため何もしない。"""
    get_supabase()


def fetch_all(table_name, columns="*", filters=None, order=None, page_size=1000):
    """Data APIの行数上限を考慮し、全行をページ単位で取得する。"""
    client = get_supabase()
    rows = []
    start = 0
    filters = filters or []
    while True:
        query = client.table(table_name).select(columns)
        for method, column, value in filters:
            query = getattr(query, method)(column, value)
        if order:
            query = query.order(order)
        result = query.range(start, start + page_size - 1).execute()
        batch = result.data or []
        rows.extend(batch)
        if len(batch) < page_size:
            break
        start += page_size
    return rows


@st.cache_data

def load_mapping():
    if not MAPPING_CSV.exists():
        return pd.DataFrame()

    df = pd.read_csv(MAPPING_CSV, dtype=str, encoding="utf-8-sig")
    required = {"survey_id", "candidate_group"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"clip_mapping.csv に必要な列がありません: {sorted(missing)}")

    df = df.drop_duplicates("survey_id").copy()
    df["candidate_group"] = (
        df["candidate_group"]
        .str.replace("_candidate", "", regex=False)
        .str.strip()
        .str.lower()
    )
    return df


@st.cache_data
def load_candidate_metadata():
    """抽出時のタグ情報を読み込み、track_idを照合用に正規化する。"""
    if not CANDIDATES_CSV.exists():
        return pd.DataFrame()

    df = pd.read_csv(CANDIDATES_CSV, dtype=str, encoding="utf-8-sig")
    required = {"track_id", "candidate_group", "genre_tags"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(
            f"dance_candidates_revised.csv に必要な列がありません: {sorted(missing)}"
        )

    def normalize_track_id(value):
        try:
            return str(int(str(value).strip()))
        except (ValueError, TypeError):
            return str(value).strip()

    df = df.copy()
    df["track_id_key"] = df["track_id"].map(normalize_track_id)
    return df.drop_duplicates("track_id_key")


def parse_json_list(value):
    try:
        parsed = json.loads(value) if value else []
        return parsed if isinstance(parsed, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def split_pipe_tags(value):
    if pd.isna(value) or not str(value).strip():
        return []
    return [tag.strip() for tag in str(value).split("|") if tag.strip()]


def build_tag_genre_relationship(responses):
    """
    抽出時タグと回答ジャンルの関係をlong形式で集計する。

    selection_count:
        複数回答の各ジャンルを1件として数える。
    normalized_vote:
        1回答の総重みが1になるよう、nジャンル選択時は各1/n票とする。
    selection_rate:
        そのタグを持つ通常回答のうち、当該ジャンルが選ばれた割合。
    """
    rows = []

    for row in responses.itertuples(index=False):
        special = getattr(row, "special_answer", None)
        if pd.notna(special) and str(special).strip():
            continue

        selected = parse_json_list(getattr(row, "selected_genres", ""))
        tags = split_pipe_tags(getattr(row, "genre_tags", ""))
        if not selected or not tags:
            continue

        weight = 1.0 / len(selected)
        for tag in tags:
            for genre in selected:
                rows.append({
                    "music_tag": tag,
                    "answered_genre": genre,
                    "selection_count": 1,
                    "normalized_vote": weight,
                })

    if not rows:
        return pd.DataFrame(columns=[
            "music_tag", "answered_genre", "tag_response_count",
            "selection_count", "normalized_vote", "selection_rate",
        ])

    long_df = pd.DataFrame(rows)
    summary = (
        long_df.groupby(["music_tag", "answered_genre"], as_index=False)
        .agg(
            selection_count=("selection_count", "sum"),
            normalized_vote=("normalized_vote", "sum"),
        )
    )

    # 分母はタグを持つ通常回答の件数。複数選択でも1回答を1件として扱う。
    denominator_rows = []
    for row in responses.itertuples(index=False):
        special = getattr(row, "special_answer", None)
        if pd.notna(special) and str(special).strip():
            continue
        selected = parse_json_list(getattr(row, "selected_genres", ""))
        if not selected:
            continue
        for tag in set(split_pipe_tags(getattr(row, "genre_tags", ""))):
            denominator_rows.append(tag)

    denominators = pd.Series(denominator_rows).value_counts().rename_axis(
        "music_tag"
    ).reset_index(name="tag_response_count")

    summary = summary.merge(denominators, on="music_tag", how="left")
    summary["selection_rate"] = (
        summary["selection_count"] / summary["tag_response_count"]
    )

    return summary[[
        "music_tag", "answered_genre", "tag_response_count",
        "selection_count", "normalized_vote", "selection_rate",
    ]].sort_values(
        ["music_tag", "normalized_vote"], ascending=[True, False]
    )


def build_tag_genre_matrix(tag_summary, value_column="normalized_vote"):
    if tag_summary.empty:
        return pd.DataFrame()
    return tag_summary.pivot_table(
        index="music_tag",
        columns="answered_genre",
        values=value_column,
        aggfunc="sum",
        fill_value=0,
    ).reset_index()


def current_utc():
    return datetime.now(timezone.utc).isoformat()


def get_audio_url(survey_id):
    base_url = str(st.secrets["SUPABASE_AUDIO_BASE_URL"]).rstrip("/")
    return f"{base_url}/{survey_id}.mp3"


def choose_assignments(mapping, participant_id):
    """5候補群から各8曲。クラウド全体で割当回数が少ない曲を優先する。"""
    rng = random.Random(participant_id)
    assignment_rows = fetch_all("assignments", "survey_id")
    if assignment_rows:
        counts = (
            pd.DataFrame(assignment_rows)["survey_id"]
            .value_counts()
            .rename_axis("survey_id")
            .reset_index(name="assigned_count")
        )
    else:
        counts = pd.DataFrame({"survey_id": mapping["survey_id"], "assigned_count": 0})

    working = mapping.merge(counts, on="survey_id", how="left")
    working["assigned_count"] = working["assigned_count"].fillna(0).astype(int)
    selected = []
    group_map = {
        "Break": "break", "Pop": "pop", "Lock": "lock",
        "House": "house", "HipHop": "hiphop",
    }
    for display_group, internal_group in group_map.items():
        group_df = working[working["candidate_group"] == internal_group].copy()
        if len(group_df) < PER_GROUP:
            raise ValueError(f"{display_group}候補が不足しています: {len(group_df)}曲")
        # 回数の少ない順。同数の曲は回答者ID由来の乱数でシャッフルする。
        group_df["tie_breaker"] = [rng.random() for _ in range(len(group_df))]
        chosen = group_df.sort_values(["assigned_count", "tie_breaker"]).head(PER_GROUP)
        selected.extend(
            {"survey_id": row.survey_id, "candidate_group": display_group}
            for row in chosen.itertuples()
        )
    rng.shuffle(selected)
    return selected


def normalize_nickname(nickname):
    return nickname.strip().casefold()


def find_participants_by_nickname(nickname):
    normalized = normalize_nickname(nickname)
    if not normalized:
        return []
    result = (
        get_supabase().table("participants")
        .select("participant_id,nickname,completed_at")
        .eq("nickname_normalized", normalized)
        .order("created_at", desc=True)
        .execute()
    )
    return [
        (row["participant_id"], row["nickname"], row.get("completed_at"))
        for row in (result.data or [])
    ]


def nickname_is_available(nickname):
    return len(find_participants_by_nickname(nickname)) == 0


def get_participant_nickname(participant_id):
    result = (
        get_supabase().table("participants")
        .select("nickname")
        .eq("participant_id", participant_id)
        .limit(1).execute()
    )
    return result.data[0]["nickname"] if result.data else ""


def get_reissue_status(participant_id):
    response_rows = fetch_all(
        "responses", "survey_id",
        [("eq", "participant_id", participant_id)],
    )
    assignment_rows = fetch_all(
        "assignments", "survey_id,answered",
        [("eq", "participant_id", participant_id)],
    )
    answered_ids = {row["survey_id"] for row in response_rows}
    pending_ids = {row["survey_id"] for row in assignment_rows if not row["answered"]}
    all_ids = [row["survey_id"] for row in assignment_rows]
    return {
        "answered_count": len(answered_ids),
        "pending_count": len(pending_ids),
        "overlap_ids": sorted(answered_ids & pending_ids),
        "duplicate_assignment_count": len(all_ids) - len(set(all_ids)),
    }


def participant_exists(participant_id):
    result = (
        get_supabase().table("participants")
        .select("participant_id")
        .eq("participant_id", participant_id.strip())
        .limit(1).execute()
    )
    return bool(result.data)


def clear_answer_session_keys():
    prefixes = ("genre_", "genre_choices_", "special_answer_")
    for key in list(st.session_state.keys()):
        if key.startswith(prefixes):
            del st.session_state[key]


def register_participant(nickname, experience_years, experienced_genres):
    participant_id = uuid.uuid4().hex[:12]
    assignments = choose_assignments(load_mapping(), participant_id)
    client = get_supabase()
    participant_record = {
        "participant_id": participant_id,
        "nickname": nickname.strip(),
        "nickname_normalized": normalize_nickname(nickname),
        "experience_years": experience_years,
        "experienced_genres": experienced_genres,
        "created_at": current_utc(),
    }
    assignment_records = [
        {
            "participant_id": participant_id,
            "position": i + 1,
            "survey_id": item["survey_id"],
            "candidate_group": item["candidate_group"],
            "answered": False,
        }
        for i, item in enumerate(assignments)
    ]
    try:
        client.table("participants").insert(participant_record).execute()
        client.table("assignments").insert(assignment_records).execute()
    except Exception:
        # 割り当て登録失敗時に回答者だけ残さない。
        client.table("participants").delete().eq("participant_id", participant_id).execute()
        raise
    return participant_id


def get_progress(participant_id):
    rows = fetch_all(
        "assignments", "answered",
        [("eq", "participant_id", participant_id)],
    )
    return len(rows), sum(bool(row["answered"]) for row in rows)


def get_next_assignment(participant_id):
    result = (
        get_supabase().table("assignments")
        .select("position,survey_id,candidate_group")
        .eq("participant_id", participant_id)
        .eq("answered", False)
        .order("position")
        .limit(1).execute()
    )
    if not result.data:
        return None
    row = result.data[0]
    return row["position"], row["survey_id"], row["candidate_group"]


def save_response(participant_id, position, survey_id, genres, special_answer):
    client = get_supabase()
    record = {
        "participant_id": participant_id,
        "survey_id": survey_id,
        "position": position,
        "selected_genres": genres,
        "special_answer": special_answer or None,
        "answered_at": current_utc(),
    }
    client.table("responses").upsert(
        record, on_conflict="participant_id,survey_id"
    ).execute()
    client.table("assignments").update({"answered": True}).eq(
        "participant_id", participant_id
    ).eq("survey_id", survey_id).execute()


def complete_participant(participant_id):
    get_supabase().table("participants").update(
        {"completed_at": current_utc()}
    ).eq("participant_id", participant_id).is_("completed_at", "null").execute()


def show_start_page():
    st.title("🎧 ダンスジャンル楽曲評価")
    st.write("20秒の音源を聴き、その曲で踊るのに適していると思うジャンルを選んでください。")

    with st.expander("回答方法と注意事項", expanded=True):
        st.markdown("""
        - 回答者ごとにランダムな40曲を提示します。
        - Break、Pop、Lock、House、HipHopは複数選択できます。
        - 5ジャンルで判断できない場合のみ、例外回答を選択してください。
        - 候補グループ、曲名、アーティスト名は表示されません。
        - 回答は1曲ごとに保存されます。
        - 実際に踊る場合に適するジャンルで判断してください。
        """)

    tab_new, tab_resume = st.tabs(["新しく回答を始める", "回答を再開する"])

    with tab_new:
        st.subheader("音声確認")
        st.audio(get_audio_url("Q0001"), format="audio/mp3")

        with st.form("participant_form"):
            audio_ok = st.checkbox("音声が聞こえ、回答できる音量であることを確認しました")
            nickname = st.text_input("ニックネーム（必須）", help="回答の再開に使用します。他の人と重ならない名前にしてください。")
            experience_years = st.selectbox(
                "ダンス経験年数", ["未経験", "1年未満", "1～3年", "4～6年", "7年以上"]
            )
            experienced_genres = st.multiselect(
                "経験したことのあるジャンル（複数選択可）", GENRES + ["その他", "なし"]
            )
            consent = st.checkbox("説明を読み、研究への回答に同意します")
            submitted = st.form_submit_button("回答を開始する", use_container_width=True)

        if submitted:
            nickname = nickname.strip()
            if not audio_ok:
                st.error("音声確認にチェックしてください。")
            elif not nickname:
                st.error("再開用ニックネームを入力してください。")
            elif not nickname_is_available(nickname):
                st.error("このニックネームは既に使用されています。別のニックネームを入力してください。")
            elif not consent:
                st.error("同意欄にチェックしてください。")
            else:
                participant_id = register_participant(nickname, experience_years, experienced_genres)
                st.session_state["participant_id"] = participant_id
                clear_answer_session_keys()
                st.rerun()

    with tab_resume:
        st.write("回答開始時に登録したニックネームを入力してください。")
        with st.form("resume_form"):
            resume_nickname = st.text_input(
                "再開用ニックネーム",
                placeholder="回答開始時と同じニックネーム",
            )
            resume = st.form_submit_button("この回答を再開する", use_container_width=True)

        if resume:
            resume_nickname = resume_nickname.strip()
            matches = find_participants_by_nickname(resume_nickname)

            if not resume_nickname:
                st.error("ニックネームを入力してください。")
            elif len(matches) == 0:
                st.error("このニックネームの回答は見つかりません。入力内容を確認してください。")
            elif len(matches) > 1:
                st.error("同じニックネームの回答が複数あります。管理者へ確認してください。")
            else:
                participant_id, stored_nickname, completed_at = matches[0]
                if completed_at:
                    st.info("このニックネームの回答は既に完了しています。")
                else:
                    st.session_state["participant_id"] = participant_id
                    clear_answer_session_keys()
                    st.rerun()

def show_question_page(participant_id):
    total, answered = get_progress(participant_id)
    assignment = get_next_assignment(participant_id)

    with st.sidebar:
        st.markdown("### 回答情報")
        st.write(f"回答ID：`{participant_id}`")
        st.write(f"回答済み：{answered} / {total}")
        nickname = get_participant_nickname(participant_id)
        if st.button("回答を中断する", use_container_width=True):
            st.session_state["confirm_pause"] = True

        if st.session_state.get("confirm_pause", False):
            st.warning("以下のニックネームで途中再開できます。")
            st.code(nickname)
            st.write("このニックネームを確認してから中断してください。")

            col_pause, col_cancel = st.columns(2)
            with col_pause:
                if st.button("確認して中断", type="primary", use_container_width=True):
                    st.session_state["paused_nickname"] = nickname
                    st.session_state["confirm_pause"] = False
                    st.session_state.pop("participant_id", None)
                    clear_answer_session_keys()
                    st.rerun()
            with col_cancel:
                if st.button("回答を続ける", use_container_width=True):
                    st.session_state["confirm_pause"] = False
                    st.rerun()

    if assignment is None:
        complete_participant(participant_id)
        st.success("回答が完了しました。ご協力ありがとうございました。")
        st.write(f"回答ID：`{participant_id}`")
        st.balloons()
        return

    position, survey_id, _candidate_group = assignment
    audio_url = get_audio_url(survey_id)

    st.progress(answered / total if total else 0)
    st.caption(f"進捗：{answered + 1} / {total}")

    with st.expander("動作確認：回答済み曲の再出題チェック", expanded=False):
        status = get_reissue_status(participant_id)
        st.write(f"回答済み曲数：{status['answered_count']}")
        st.write(f"未回答曲数：{status['pending_count']}")
        st.write(f"割り当て内の重複数：{status['duplicate_assignment_count']}")
        if status["overlap_ids"]:
            st.error(
                "回答済みなのに未回答扱いの曲があります："
                + ", ".join(status["overlap_ids"])
            )
        else:
            st.success("回答済み曲が未回答として再出題される状態はありません。")

        st.caption(f"現在表示中の音源ID：{survey_id}")
    st.subheader("音源を再生して回答してください")

    st.audio(audio_url, format="audio/mp3")

    st.markdown("**この曲で踊るのに適していると思うジャンルをすべて選んでください。**")

    st.markdown("**当てはまるジャンル（複数選択可）**")
    genre_choices = []
    for genre in GENRES:
        if st.checkbox(genre, key=f"genre_{survey_id}_{genre}"):
            genre_choices.append(genre)

    special_answer = st.radio(
        "5ジャンルで回答できない場合",
        ["", *SPECIAL_OPTIONS],
        format_func=lambda x: "選択しない" if x == "" else x,
        key=f"special_answer_{survey_id}",
    )

    if genre_choices and special_answer:
        st.warning(
            "ジャンル選択と「当てはまらない／判断できない」は同時に選べません。"
        )

    if st.button("回答を保存して次の曲へ", type="primary", use_container_width=True):
        if not genre_choices and not special_answer:
            st.error("ジャンルまたは特別回答を選択してください。")
        elif genre_choices and special_answer:
            st.error("ジャンル選択と特別回答のどちらか一方にしてください。")
        else:
            save_response(
                participant_id,
                position,
                survey_id,
                genre_choices,
                special_answer,
            )
            st.rerun()



def show_admin_download():
    st.divider()
    with st.expander("管理者向け：回答CSVの出力"):
        st.caption("管理者パスワードを入力し、認証ボタンを押してください。")

        # 認証状態はブラウザのセッション内で保持する。
        if "admin_authenticated" not in st.session_state:
            st.session_state["admin_authenticated"] = False

        # secrets.tomlがない場合でも、分かりやすいエラーを表示する。
        try:
            configured_password = str(st.secrets["ADMIN_PASSWORD"])
        except Exception:
            configured_password = ""

        if not configured_password:
            st.error(
                ".streamlit/secrets.toml に ADMIN_PASSWORD が設定されていません。"
            )
            st.code('ADMIN_PASSWORD = "任意の管理者パスワード"', language="toml")
            return

        if not st.session_state["admin_authenticated"]:
            with st.form("admin_login_form"):
                password = st.text_input(
                    "管理者パスワード",
                    type="password",
                    key="admin_password_input",
                )
                login = st.form_submit_button(
                    "認証する",
                    use_container_width=True,
                )

            if login:
                if password == configured_password:
                    st.session_state["admin_authenticated"] = True
                    st.rerun()
                else:
                    st.error("管理者パスワードが一致しません。")
            return

        participant_rows = fetch_all(
            "participants",
            "participant_id,nickname,experience_years,experienced_genres,created_at,completed_at",
            order="created_at",
        )
        response_rows = fetch_all(
            "responses",
            "participant_id,survey_id,position,selected_genres,special_answer,answered_at",
            order="answered_at",
        )
        participants = pd.DataFrame(participant_rows)
        responses = pd.DataFrame(response_rows)

        participant_columns = [
            "participant_id", "nickname", "experience_years",
            "experienced_genres", "created_at", "completed_at",
        ]
        response_columns = [
            "participant_id", "survey_id", "position", "selected_genres",
            "special_answer", "answered_at",
        ]
        if participants.empty:
            participants = pd.DataFrame(columns=participant_columns)
        if responses.empty:
            responses = pd.DataFrame(columns=response_columns)

        # JSONB配列を既存のCSV表現へ変換して分析処理との互換性を保つ。
        participants["experienced_genres"] = participants["experienced_genres"].apply(
            lambda value: json.dumps(value or [], ensure_ascii=False)
        )
        responses["selected_genres"] = responses["selected_genres"].apply(
            lambda value: json.dumps(value or [], ensure_ascii=False)
        )
        responses = responses.merge(
            participants[["participant_id", "nickname", "experience_years", "experienced_genres"]],
            on="participant_id", how="left", validate="many_to_one",
        )

        mapping_for_export = load_mapping().copy()
        display_name_map = {
            "break": "Break", "pop": "Pop", "lock": "Lock",
            "house": "House", "hiphop": "HipHop",
        }
        mapping_for_export["candidate_group"] = (
            mapping_for_export["candidate_group"].map(display_name_map)
            .fillna(mapping_for_export["candidate_group"])
        )
        export_columns = ["survey_id", "candidate_group"]
        if "track_id" in mapping_for_export.columns:
            export_columns.append("track_id")
        responses = responses.merge(
            mapping_for_export[export_columns], on="survey_id", how="left",
            validate="many_to_one",
        )

        candidate_metadata = load_candidate_metadata()
        if candidate_metadata.empty:
            st.warning("data/dance_candidates_revised.csv がないため、抽出時タグとの関係は表示できません。")
            for column in ["genre_tags", "matched_primary_tags", "matched_secondary_tags", "match_score"]:
                responses[column] = ""
        elif not responses.empty and "track_id" in responses.columns:
            def normalize_track_id_for_merge(value):
                try:
                    return str(int(str(value).strip()))
                except (ValueError, TypeError):
                    return str(value).strip()
            responses["track_id_key"] = responses["track_id"].map(normalize_track_id_for_merge)
            metadata_columns = [
                "track_id_key", "genre_tags", "matched_primary_tags",
                "matched_secondary_tags", "match_score",
            ]
            metadata_columns = [c for c in metadata_columns if c in candidate_metadata.columns]
            responses = responses.merge(
                candidate_metadata[metadata_columns], on="track_id_key",
                how="left", validate="many_to_one",
            ).drop(columns=["track_id_key"])

        if not responses.empty:
            responses["candidate_selected"] = responses.apply(
                lambda row: row["candidate_group"] in parse_json_list(row["selected_genres"]),
                axis=1,
            )
            responses["agreement_type"] = responses.apply(
                lambda row: (
                    "判断不能" if row["special_answer"] == "判断できない"
                    else "該当なし" if row["special_answer"] == "どのジャンルにも当てはまらない"
                    else "候補を含む複数回答" if row["candidate_selected"] and len(parse_json_list(row["selected_genres"])) >= 2
                    else "候補と単独一致" if row["candidate_selected"]
                    else "候補と不一致"
                ), axis=1,
            )
        else:
            responses["candidate_selected"] = pd.Series(dtype=bool)
            responses["agreement_type"] = pd.Series(dtype=str)

        preferred_columns = [
            "participant_id", "nickname", "experience_years", "experienced_genres",
            "survey_id", "track_id", "candidate_group", "genre_tags",
            "matched_primary_tags", "matched_secondary_tags", "match_score",
            "position", "selected_genres", "candidate_selected", "agreement_type",
            "special_answer", "answered_at",
        ]
        responses = responses[[c for c in preferred_columns if c in responses.columns]]

        if response_rows:
            answer_counts = pd.DataFrame(response_rows).groupby("participant_id").size()
        else:
            answer_counts = pd.Series(dtype=int)
        progress = participants[["participant_id", "nickname", "created_at", "completed_at"]].copy()
        progress["answered_count"] = progress["participant_id"].map(answer_counts).fillna(0).astype(int)
        progress = progress[["participant_id", "nickname", "answered_count", "created_at", "completed_at"]]

        st.success("管理者認証済み")
        col1, col2, col3 = st.columns(3)
        col1.metric("保存済み回答数", len(responses))
        col2.metric("回答者数", progress["participant_id"].nunique())

        valid_for_agreement = responses[
            responses["special_answer"].fillna("") == ""
        ]
        agreement_rate = (
            valid_for_agreement["candidate_selected"].mean()
            if len(valid_for_agreement) else 0.0
        )
        col3.metric("候補を含む回答率", f"{agreement_rate:.1%}")

        st.caption(
            "候補を含む回答率は、判断不能・該当なしを除き、回答に候補グループが含まれた割合です。"
        )

        if not responses.empty:
            group_summary = (
                responses.assign(
                    valid_answer=responses["special_answer"].fillna("").eq(""),
                    candidate_hit=responses["candidate_selected"].astype(bool),
                )
                .groupby("candidate_group", dropna=False)
                .agg(
                    response_count=("survey_id", "size"),
                    valid_answer_count=("valid_answer", "sum"),
                    candidate_selected_count=("candidate_hit", "sum"),
                )
                .reset_index()
            )
            group_summary["candidate_selected_rate"] = (
                group_summary["candidate_selected_count"]
                / group_summary["valid_answer_count"].replace(0, pd.NA)
            )

            st.dataframe(
                group_summary,
                use_container_width=True,
                hide_index=True,
            )
        else:
            group_summary = pd.DataFrame()

        # 抽出時タグと回答ジャンルの関係を集計する。
        tag_genre_summary = build_tag_genre_relationship(responses)
        tag_genre_matrix = build_tag_genre_matrix(
            tag_genre_summary, value_column="normalized_vote"
        )

        if not tag_genre_summary.empty:
            st.subheader("抽出時タグと回答ジャンルの関係")
            st.caption(
                "normalized_voteは、複数ジャンル回答を1人合計1票に正規化した値です。"
            )

            available_tags = sorted(tag_genre_summary["music_tag"].unique())
            selected_tag = st.selectbox(
                "確認する音楽タグ",
                available_tags,
                key="admin_music_tag_filter",
            )
            selected_tag_table = tag_genre_summary[
                tag_genre_summary["music_tag"] == selected_tag
            ].sort_values("normalized_vote", ascending=False)
            st.dataframe(
                selected_tag_table,
                use_container_width=True,
                hide_index=True,
            )

        st.download_button(
            "responses_with_candidate_group_and_tags.csvをダウンロード",
            responses.to_csv(index=False).encode("utf-8-sig"),
            file_name="responses_with_candidate_group_and_tags.csv",
            mime="text/csv",
            use_container_width=True,
        )

        if not group_summary.empty:
            st.download_button(
                "candidate_group_summary.csvをダウンロード",
                group_summary.to_csv(index=False).encode("utf-8-sig"),
                file_name="candidate_group_summary.csv",
                mime="text/csv",
                use_container_width=True,
            )

        if not tag_genre_summary.empty:
            st.download_button(
                "tag_genre_relationship.csvをダウンロード",
                tag_genre_summary.to_csv(index=False).encode("utf-8-sig"),
                file_name="tag_genre_relationship.csv",
                mime="text/csv",
                use_container_width=True,
            )

            st.download_button(
                "tag_genre_matrix.csvをダウンロード",
                tag_genre_matrix.to_csv(index=False).encode("utf-8-sig"),
                file_name="tag_genre_matrix.csv",
                mime="text/csv",
                use_container_width=True,
            )

        st.download_button(
            "participant_progress.csvをダウンロード",
            progress.to_csv(index=False).encode("utf-8-sig"),
            file_name="participant_progress.csv",
            mime="text/csv",
            use_container_width=True,
        )

        if st.button("管理者ログアウト", use_container_width=True):
            st.session_state["admin_authenticated"] = False
            st.session_state.pop("admin_password_input", None)
            st.rerun()


def main():
    init_database()
    mapping = load_mapping()

    if mapping.empty:
        st.error("data/clip_mapping.csv がありません。")
        st.stop()

    admin_mode = str(st.query_params.get("admin", "")) == "1"
    if admin_mode:
        st.title("アンケート管理画面")
        show_admin_download()
        return

    participant_id = st.session_state.get("participant_id")
    if participant_id and participant_exists(participant_id):
        show_question_page(participant_id)
    else:
        st.session_state.pop("participant_id", None)
        show_start_page()


if __name__ == "__main__":
    main()
