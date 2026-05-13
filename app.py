# app.py
# Sistema simples de vocabulário controlado em Streamlit.
# Permite cadastrar termos, criar hierarquias, registrar notas, comentários,
# termos relacionados e exportar/importar CSV.

from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd
import streamlit as st

DB_PATH = Path(os.getenv("VOCAB_DB_PATH", "vocabulario.db"))
STATUS = ["Ativo", "Em revisão", "Substituído", "Inativo"]


# ---------------------------------------------------------------------
# Banco de dados
# ---------------------------------------------------------------------

def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.execute("PRAGMA journal_mode = WAL")
    c.execute("PRAGMA busy_timeout = 30000")
    return c


def init_db() -> None:
    with conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS terms (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                term TEXT NOT NULL UNIQUE,
                parent_id INTEGER,
                status TEXT NOT NULL DEFAULT 'Ativo',
                scope_note TEXT DEFAULT '',
                history_note TEXT DEFAULT '',
                editorial_note TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(parent_id) REFERENCES terms(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS related_terms (
                term_id INTEGER NOT NULL,
                related_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(term_id, related_id),
                CHECK(term_id <> related_id),
                FOREIGN KEY(term_id) REFERENCES terms(id) ON DELETE CASCADE,
                FOREIGN KEY(related_id) REFERENCES terms(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                term_id INTEGER NOT NULL,
                author TEXT DEFAULT '',
                comment TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(term_id) REFERENCES terms(id) ON DELETE CASCADE
            );
            """
        )


def terms_df() -> pd.DataFrame:
    with conn() as c:
        return pd.read_sql_query(
            """
            SELECT
                t.id,
                t.term,
                t.parent_id,
                p.term AS parent_term,
                t.status,
                t.scope_note,
                t.history_note,
                t.editorial_note,
                t.created_at,
                t.updated_at
            FROM terms t
            LEFT JOIN terms p ON p.id = t.parent_id
            ORDER BY lower(t.term)
            """,
            c,
        )


def fetch_term(term_id: int) -> sqlite3.Row | None:
    with conn() as c:
        return c.execute("SELECT * FROM terms WHERE id = ?", (term_id,)).fetchone()


def related_ids(term_id: int) -> list[int]:
    with conn() as c:
        rows = c.execute(
            "SELECT related_id FROM related_terms WHERE term_id = ?",
            (term_id,),
        ).fetchall()
    return [int(r["related_id"]) for r in rows]


def comments_df(term_id: int) -> pd.DataFrame:
    with conn() as c:
        return pd.read_sql_query(
            """
            SELECT author, comment, created_at
            FROM comments
            WHERE term_id = ?
            ORDER BY created_at DESC, id DESC
            """,
            c,
            params=(term_id,),
        )


# ---------------------------------------------------------------------
# Regras
# ---------------------------------------------------------------------

def name_exists(term: str, current_id: Optional[int] = None) -> bool:
    sql = "SELECT id FROM terms WHERE lower(term) = lower(?)"
    params: tuple = (term.strip(),)
    if current_id is not None:
        sql += " AND id <> ?"
        params = (term.strip(), current_id)
    with conn() as c:
        return c.execute(sql, params).fetchone() is not None


def creates_cycle(term_id: int, parent_id: Optional[int]) -> bool:
    """Evita que um termo vire pai de si mesmo ou de um ancestral."""
    if parent_id is None:
        return False
    current = parent_id
    seen: set[int] = set()
    with conn() as c:
        while current is not None:
            if current == term_id or current in seen:
                return True
            seen.add(current)
            row = c.execute("SELECT parent_id FROM terms WHERE id = ?", (current,)).fetchone()
            current = None if row is None else row["parent_id"]
    return False


def save_related(c: sqlite3.Connection, term_id: int, ids: list[int]) -> None:
    old = c.execute(
        "SELECT related_id FROM related_terms WHERE term_id = ?",
        (term_id,),
    ).fetchall()
    for row in old:
        old_id = int(row["related_id"])
        c.execute(
            """
            DELETE FROM related_terms
            WHERE (term_id = ? AND related_id = ?)
               OR (term_id = ? AND related_id = ?)
            """,
            (term_id, old_id, old_id, term_id),
        )

    for rid in ids:
        if rid == term_id:
            continue
        c.execute(
            "INSERT OR IGNORE INTO related_terms VALUES (?, ?, ?)",
            (term_id, rid, now()),
        )
        c.execute(
            "INSERT OR IGNORE INTO related_terms VALUES (?, ?, ?)",
            (rid, term_id, now()),
        )


def create_term(
    term: str,
    parent_id: Optional[int],
    status: str,
    scope_note: str,
    history_note: str,
    editorial_note: str,
    rel_ids: list[int],
) -> None:
    term = term.strip()
    if not term:
        raise ValueError("Informe o termo.")
    if name_exists(term):
        raise ValueError("Já existe um termo com esse nome.")

    with conn() as c:
        ts = now()
        cur = c.execute(
            """
            INSERT INTO terms
            (term, parent_id, status, scope_note, history_note, editorial_note, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (term, parent_id, status, scope_note, history_note, editorial_note, ts, ts),
        )
        save_related(c, int(cur.lastrowid), rel_ids)
        c.commit()


def update_term(
    term_id: int,
    term: str,
    parent_id: Optional[int],
    status: str,
    scope_note: str,
    history_note: str,
    editorial_note: str,
    rel_ids: list[int],
) -> None:
    term = term.strip()
    if not term:
        raise ValueError("Informe o termo.")
    if name_exists(term, current_id=term_id):
        raise ValueError("Já existe outro termo com esse nome.")
    if parent_id == term_id or creates_cycle(term_id, parent_id):
        raise ValueError("A hierarquia ficaria circular. Escolha outro termo pai.")

    with conn() as c:
        c.execute(
            """
            UPDATE terms
            SET term = ?, parent_id = ?, status = ?, scope_note = ?,
                history_note = ?, editorial_note = ?, updated_at = ?
            WHERE id = ?
            """,
            (term, parent_id, status, scope_note, history_note, editorial_note, now(), term_id),
        )
        save_related(c, term_id, rel_ids)
        c.commit()


def delete_term(term_id: int) -> None:
    with conn() as c:
        c.execute("DELETE FROM terms WHERE id = ?", (term_id,))
        c.commit()


def add_comment(term_id: int, author: str, comment: str) -> None:
    if not comment.strip():
        raise ValueError("Digite o comentário.")
    with conn() as c:
        c.execute(
            "INSERT INTO comments (term_id, author, comment, created_at) VALUES (?, ?, ?, ?)",
            (term_id, author.strip(), comment.strip(), now()),
        )
        c.commit()


# ---------------------------------------------------------------------
# Interface: helpers
# ---------------------------------------------------------------------

def option_map(df: pd.DataFrame, *, include_empty=True, exclude_id: Optional[int] = None) -> dict[str, Optional[int]]:
    opts: dict[str, Optional[int]] = {}
    if include_empty:
        opts["— Sem termo pai —"] = None
    for r in df.itertuples(index=False):
        if exclude_id is not None and int(r.id) == exclude_id:
            continue
        opts[str(r.term)] = int(r.id)
    return opts


def render_tree(df: pd.DataFrame, parent_id: Optional[int] = None, level: int = 0) -> None:
    if df.empty and level == 0:
        st.info("Nenhum termo cadastrado ainda.")
        return
    children = df[df["parent_id"].isna()] if parent_id is None else df[df["parent_id"] == parent_id]
    children = children.sort_values("term", key=lambda s: s.str.lower())
    for _, row in children.iterrows():
        indent = "&nbsp;" * 4 * level
        status = "" if row["status"] == "Ativo" else f" <small>({row['status']})</small>"
        st.markdown(f"{indent}- **{row['term']}**{status}", unsafe_allow_html=True)
        render_tree(df, int(row["id"]), level + 1)


def export_df(df: pd.DataFrame) -> pd.DataFrame:
    cols = {
        "term": "termo",
        "parent_term": "termo_pai",
        "status": "situacao",
        "scope_note": "nota_explicativa",
        "history_note": "nota_historica",
        "editorial_note": "comentario_editorial",
        "created_at": "criado_em",
        "updated_at": "atualizado_em",
    }
    if df.empty:
        return pd.DataFrame(columns=cols.values())
    return df[list(cols)].rename(columns=cols)


def import_terms(upload) -> tuple[int, int]:
    data = pd.read_csv(upload).fillna("")
    data.columns = [c.strip().lower() for c in data.columns]
    if "termo" not in data.columns:
        raise ValueError("O CSV precisa ter uma coluna chamada 'termo'.")

    created = updated = 0

    # Primeiro cria/atualiza termos sem aplicar hierarquia.
    for _, row in data.iterrows():
        termo = str(row.get("termo", "")).strip()
        if not termo:
            continue
        situacao = str(row.get("situacao", "Ativo")).strip() or "Ativo"
        if situacao not in STATUS:
            situacao = "Ativo"
        scope = str(row.get("nota_explicativa", ""))
        hist = str(row.get("nota_historica", ""))
        edit = str(row.get("comentario_editorial", ""))

        df = terms_df()
        found = df[df["term"].str.lower() == termo.lower()]
        if found.empty:
            create_term(termo, None, situacao, scope, hist, edit, [])
            created += 1
        else:
            tid = int(found.iloc[0]["id"])
            current = fetch_term(tid)
            update_term(tid, termo, current["parent_id"], situacao, scope, hist, edit, related_ids(tid))
            updated += 1

    # Depois aplica termo pai.
    df = terms_df()
    ids = {r.term.lower(): int(r.id) for r in df.itertuples(index=False)}
    for _, row in data.iterrows():
        termo = str(row.get("termo", "")).strip().lower()
        pai = str(row.get("termo_pai", "")).strip().lower()
        if not termo or not pai or termo not in ids or pai not in ids:
            continue
        tid, pid = ids[termo], ids[pai]
        current = fetch_term(tid)
        if current and tid != pid and not creates_cycle(tid, pid):
            update_term(
                tid,
                current["term"],
                pid,
                current["status"],
                current["scope_note"],
                current["history_note"],
                current["editorial_note"],
                related_ids(tid),
            )
    return created, updated


# ---------------------------------------------------------------------
# App
# ---------------------------------------------------------------------

st.set_page_config(page_title="Vocabulário controlado", page_icon="📚", layout="wide")
init_db()
df = terms_df()

st.title("📚 Vocabulário controlado")
st.caption("Cadastro online simples de termos, hierarquias, notas e comentários.")

with st.sidebar:
    st.subheader("Como entrar com os termos")
    st.markdown(
        """
        1. Entre na aba **Cadastrar termo**.  
        2. Digite o **termo**.  
        3. Escolha o **termo pai**, se houver.  
        4. Preencha as notas.  
        5. Clique em **Salvar termo**.
        """
    )
    st.divider()
    st.write(f"Banco: `{DB_PATH}`")
    st.write(f"Total de termos: **{len(df)}**")

abas = st.tabs(["Cadastrar termo", "Consultar", "Editar/excluir", "Hierarquia", "Comentários", "Importar/exportar"])

# Cadastrar
with abas[0]:
    st.header("Cadastrar novo termo")
    pais = option_map(df)
    relacionados = option_map(df, include_empty=False)
    with st.form("novo_termo", clear_on_submit=True):
        termo = st.text_input("Termo *", placeholder="Ex.: Processo legislativo")
        pai_label = st.selectbox("Termo pai", list(pais))
        situacao = st.selectbox("Situação", STATUS)
        nota = st.text_area("Nota explicativa / nota de escopo")
        historico = st.text_area("Nota histórica")
        comentario_editorial = st.text_area("Comentário editorial")
        rel_labels = st.multiselect("Termos relacionados", list(relacionados))
        salvar = st.form_submit_button("Salvar termo", type="primary")

    if salvar:
        try:
            create_term(
                termo,
                pais[pai_label],
                situacao,
                nota,
                historico,
                comentario_editorial,
                [relacionados[x] for x in rel_labels if relacionados[x] is not None],
            )
            st.success("Termo salvo com sucesso.")
            st.rerun()
        except Exception as e:
            st.error(str(e))

# Consultar
with abas[1]:
    st.header("Consultar termos")
    busca = st.text_input("Buscar por termo, nota, histórico ou comentário")
    filtrado = df.copy()
    if busca.strip() and not filtrado.empty:
        alvo = busca.strip().lower()
        mask = False
        for col in ["term", "parent_term", "scope_note", "history_note", "editorial_note", "status"]:
            mask = mask | filtrado[col].fillna("").str.lower().str.contains(alvo, regex=False)
        filtrado = filtrado[mask]

    if filtrado.empty:
        st.info("Nenhum termo encontrado.")
    else:
        view = export_df(filtrado)
        st.dataframe(view, use_container_width=True, hide_index=True)

        st.subheader("Ficha do termo")
        escolhido = st.selectbox("Selecione um termo", filtrado["term"].tolist())
        row = filtrado[filtrado["term"] == escolhido].iloc[0]
        tid = int(row["id"])
        st.markdown(f"**Termo:** {row['term']}")
        st.markdown(f"**Termo pai:** {row['parent_term'] or '—'}")
        st.markdown(f"**Situação:** {row['status']}")

        rels = related_ids(tid)
        nomes_rel = df[df["id"].isin(rels)]["term"].tolist()
        st.markdown(f"**Termos relacionados:** {'; '.join(nomes_rel) if nomes_rel else '—'}")
        st.markdown("**Nota explicativa / escopo**")
        st.write(row["scope_note"] or "—")
        st.markdown("**Nota histórica**")
        st.write(row["history_note"] or "—")
        st.markdown("**Comentário editorial**")
        st.write(row["editorial_note"] or "—")

# Editar/excluir
with abas[2]:
    st.header("Editar ou excluir termo")
    if df.empty:
        st.info("Cadastre um termo primeiro.")
    else:
        escolhido = st.selectbox("Termo a editar", df["term"].tolist(), key="editar")
        tid = int(df.loc[df["term"] == escolhido, "id"].iloc[0])
        atual = fetch_term(tid)
        pais = option_map(df, exclude_id=tid)
        relacionados = option_map(df, include_empty=False, exclude_id=tid)

        pai_atual = "— Sem termo pai —"
        if atual and atual["parent_id"]:
            nome_pai = df.loc[df["id"] == atual["parent_id"], "term"]
            if not nome_pai.empty:
                pai_atual = nome_pai.iloc[0]

        rel_atual = [nome for nome, rid in relacionados.items() if rid in related_ids(tid)]

        with st.form("editar_termo"):
            termo = st.text_input("Termo *", value=atual["term"])
            pai_label = st.selectbox("Termo pai", list(pais), index=list(pais).index(pai_atual) if pai_atual in pais else 0)
            situacao = st.selectbox("Situação", STATUS, index=STATUS.index(atual["status"]) if atual["status"] in STATUS else 0)
            nota = st.text_area("Nota explicativa / nota de escopo", value=atual["scope_note"])
            historico = st.text_area("Nota histórica", value=atual["history_note"])
            comentario_editorial = st.text_area("Comentário editorial", value=atual["editorial_note"])
            rel_labels = st.multiselect("Termos relacionados", list(relacionados), default=rel_atual)
            salvar = st.form_submit_button("Salvar alterações", type="primary")

        if salvar:
            try:
                update_term(
                    tid,
                    termo,
                    pais[pai_label],
                    situacao,
                    nota,
                    historico,
                    comentario_editorial,
                    [relacionados[x] for x in rel_labels if relacionados[x] is not None],
                )
                st.success("Termo atualizado.")
                st.rerun()
            except Exception as e:
                st.error(str(e))

        st.divider()
        st.subheader("Excluir")
        st.warning("Ao excluir, os comentários e relações desse termo serão removidos. Termos filhos ficarão sem termo pai.")
        confirma = st.checkbox("Confirmo que quero excluir este termo")
        if st.button("Excluir termo", disabled=not confirma):
            delete_term(tid)
            st.success("Termo excluído.")
            st.rerun()

# Hierarquia
with abas[3]:
    st.header("Hierarquia")
    st.write("A árvore abaixo é montada a partir do campo **Termo pai**.")
    render_tree(df)

# Comentários
with abas[4]:
    st.header("Comentários")
    if df.empty:
        st.info("Cadastre um termo primeiro.")
    else:
        escolhido = st.selectbox("Termo", df["term"].tolist(), key="comentarios")
        tid = int(df.loc[df["term"] == escolhido, "id"].iloc[0])
        with st.form("novo_comentario", clear_on_submit=True):
            autor = st.text_input("Autor", placeholder="Ex.: Equipe GDI")
            comentario = st.text_area("Comentário")
            salvar_comentario = st.form_submit_button("Adicionar comentário", type="primary")
        if salvar_comentario:
            try:
                add_comment(tid, autor, comentario)
                st.success("Comentário adicionado.")
                st.rerun()
            except Exception as e:
                st.error(str(e))

        cdf = comments_df(tid)
        if cdf.empty:
            st.info("Nenhum comentário registrado para este termo.")
        else:
            for r in cdf.itertuples(index=False):
                st.markdown(f"**{r.author or 'Sem autor'}** — {r.created_at}")
                st.write(r.comment)
                st.divider()

# Importar/exportar
with abas[5]:
    st.header("Importar/exportar")
    out = export_df(df)

    st.subheader("Exportar")
    st.download_button(
        "Baixar CSV",
        data=out.to_csv(index=False).encode("utf-8-sig"),
        file_name="vocabulario.csv",
        mime="text/csv",
        disabled=out.empty,
    )

    st.subheader("Importar CSV")
    st.write("Colunas aceitas: `termo`, `termo_pai`, `situacao`, `nota_explicativa`, `nota_historica`, `comentario_editorial`.")
    upload = st.file_uploader("Selecionar CSV", type="csv")
    if upload is not None and st.button("Importar/atualizar"):
        try:
            criados, atualizados = import_terms(upload)
            st.success(f"Importação concluída. Criados: {criados}. Atualizados: {atualizados}.")
            st.rerun()
        except Exception as e:
            st.error(str(e))

    modelo = pd.DataFrame(
        [
            {
                "termo": "Processo legislativo",
                "termo_pai": "",
                "situacao": "Ativo",
                "nota_explicativa": "Conjunto de atos relativos à tramitação de proposições.",
                "nota_historica": "",
                "comentario_editorial": "",
            },
            {
                "termo": "Projeto de lei",
                "termo_pai": "Processo legislativo",
                "situacao": "Ativo",
                "nota_explicativa": "Espécie de proposição legislativa.",
                "nota_historica": "",
                "comentario_editorial": "",
            },
        ]
    )
    with st.expander("Ver modelo de CSV"):
        st.dataframe(modelo, use_container_width=True, hide_index=True)
        st.download_button(
            "Baixar modelo CSV",
            data=modelo.to_csv(index=False).encode("utf-8-sig"),
            file_name="modelo_vocabulario.csv",
            mime="text/csv",
        )
