from __future__ import annotations

import base64
import copy
import csv
import html
import io
import json
import uuid
from datetime import datetime, timezone
from typing import Any

import requests
import streamlit as st


APP_TITLE = "Vocabulário controlado"
STATUS = ["Ativo", "Em revisão", "Substituído", "Inativo"]
DEFAULT_DATA = {"schema_version": 1, "terms": []}


# -----------------------------------------------------------------------------
# Configuração e acesso ao GitHub
# -----------------------------------------------------------------------------


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_secret(name: str, default: str = "") -> str:
    """Lê segredo do Streamlit Cloud sem quebrar quando ele ainda não existe."""
    try:
        value = st.secrets.get(name, default)
        if value is None:
            return default
        return str(value)
    except Exception:
        return default


def github_config() -> dict[str, str]:
    return {
        "token": get_secret("GITHUB_TOKEN"),
        "repo": get_secret("GITHUB_REPO"),  # formato: usuario-ou-org/nome-do-repositorio
        "branch": get_secret("GITHUB_BRANCH", "main"),
        "path": get_secret("DATA_PATH", "data/vocabulario.json"),
    }


def github_headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def ensure_github_config_or_stop() -> dict[str, str]:
    cfg = github_config()
    missing = [k for k in ["GITHUB_TOKEN", "GITHUB_REPO"] if not get_secret(k)]
    if missing:
        st.error("Faltam configurações em Secrets do Streamlit Cloud.")
        st.markdown(
            """
            Cadastre estes valores em **App settings > Secrets** no Streamlit Cloud:

            ```toml
            GITHUB_TOKEN = "cole_aqui_o_token_do_github"
            GITHUB_REPO = "usuario-ou-org/nome-do-repositorio"
            GITHUB_BRANCH = "main"
            DATA_PATH = "data/vocabulario.json"
            ```

            O token do GitHub precisa ter permissão de **Contents: Read and write** no repositório.
            """
        )
        st.stop()
    return cfg


def github_content_url(cfg: dict[str, str]) -> str:
    return f"https://api.github.com/repos/{cfg['repo']}/contents/{cfg['path']}"


def load_data_from_github(cfg: dict[str, str]) -> tuple[dict[str, Any], str | None]:
    """Retorna (dados, sha_do_arquivo). Se o arquivo ainda não existe, retorna dados vazios."""
    url = github_content_url(cfg)
    params = {"ref": cfg["branch"]}
    response = requests.get(url, headers=github_headers(cfg["token"]), params=params, timeout=30)

    if response.status_code == 404:
        return copy.deepcopy(DEFAULT_DATA), None

    if not response.ok:
        raise RuntimeError(
            f"Não foi possível ler o arquivo no GitHub. Código {response.status_code}: {response.text[:500]}"
        )

    payload = response.json()
    encoded = payload.get("content", "")
    sha = payload.get("sha")

    try:
        raw = base64.b64decode(encoded).decode("utf-8")
        data = json.loads(raw)
    except Exception as exc:
        raise RuntimeError("O arquivo JSON do vocabulário existe, mas não pôde ser lido.") from exc

    if "terms" not in data or not isinstance(data["terms"], list):
        raise RuntimeError("O arquivo JSON não tem o formato esperado: campo 'terms' ausente ou inválido.")

    return data, sha


def save_data_to_github(
    cfg: dict[str, str],
    data: dict[str, Any],
    sha: str | None,
    message: str,
) -> None:
    """Cria ou atualiza o arquivo JSON no GitHub."""
    url = github_content_url(cfg)
    content = json.dumps(data, ensure_ascii=False, indent=2)
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")

    body: dict[str, Any] = {
        "message": message,
        "content": encoded,
        "branch": cfg["branch"],
    }
    if sha:
        body["sha"] = sha

    response = requests.put(url, headers=github_headers(cfg["token"]), json=body, timeout=30)

    if response.status_code == 409:
        raise RuntimeError(
            "Conflito ao salvar: outra pessoa alterou o vocabulário ao mesmo tempo. "
            "Recarregue a página e tente salvar novamente."
        )

    if not response.ok:
        raise RuntimeError(
            f"Não foi possível salvar no GitHub. Código {response.status_code}: {response.text[:700]}"
        )


# -----------------------------------------------------------------------------
# Funções de dados
# -----------------------------------------------------------------------------


def normalize_text(value: str) -> str:
    return " ".join((value or "").strip().split())


def sorted_terms(data: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(data.get("terms", []), key=lambda item: item.get("term", "").casefold())


def get_term(data: dict[str, Any], term_id: str | None) -> dict[str, Any] | None:
    if not term_id:
        return None
    for term in data.get("terms", []):
        if term.get("id") == term_id:
            return term
    return None


def term_label(term: dict[str, Any]) -> str:
    return term.get("term", "Sem termo")


def term_label_with_status(term: dict[str, Any]) -> str:
    status = term.get("status") or "Ativo"
    return f"{term_label(term)} [{status}]"


def term_name_by_id(data: dict[str, Any], term_id: str | None) -> str:
    term = get_term(data, term_id)
    return term_label(term) if term else ""


def label_to_id_map(data: dict[str, Any], include_empty: bool = True, exclude_id: str | None = None) -> dict[str, str | None]:
    items: dict[str, str | None] = {}
    if include_empty:
        items["— sem termo pai —"] = None
    for term in sorted_terms(data):
        if exclude_id and term.get("id") == exclude_id:
            continue
        label = term_label_with_status(term)
        items[label] = term.get("id")
    return items


def children_of(data: dict[str, Any], parent_id: str | None) -> list[dict[str, Any]]:
    return sorted(
        [term for term in data.get("terms", []) if term.get("parent_id") == parent_id],
        key=lambda item: item.get("term", "").casefold(),
    )


def is_descendant(data: dict[str, Any], possible_child_id: str | None, possible_parent_id: str | None) -> bool:
    """Verifica se possible_child_id está abaixo de possible_parent_id na árvore."""
    current = possible_child_id
    seen: set[str] = set()
    while current:
        if current == possible_parent_id:
            return True
        if current in seen:
            return False
        seen.add(current)
        term = get_term(data, current)
        current = term.get("parent_id") if term else None
    return False


def duplicate_term_exists(data: dict[str, Any], term_text: str, ignore_id: str | None = None) -> bool:
    normalized = normalize_text(term_text).casefold()
    for term in data.get("terms", []):
        if ignore_id and term.get("id") == ignore_id:
            continue
        if normalize_text(term.get("term", "")).casefold() == normalized:
            return True
    return False


def upsert_term(
    data: dict[str, Any],
    *,
    term_id: str | None,
    term_text: str,
    parent_id: str | None,
    status: str,
    scope_note: str,
    history_note: str,
    editorial_comment: str,
    related_ids: list[str],
    user_name: str,
) -> str:
    term_text = normalize_text(term_text)
    if not term_text:
        raise ValueError("Informe o termo.")

    if duplicate_term_exists(data, term_text, ignore_id=term_id):
        raise ValueError("Já existe um termo com esse nome.")

    if term_id and parent_id == term_id:
        raise ValueError("Um termo não pode ser pai dele mesmo.")

    if term_id and parent_id and is_descendant(data, parent_id, term_id):
        raise ValueError("Essa hierarquia criaria um ciclo. Escolha outro termo pai.")

    clean_related = []
    for rid in related_ids:
        if rid and rid != term_id and rid not in clean_related:
            clean_related.append(rid)

    timestamp = now_iso()
    existing = get_term(data, term_id)

    if existing:
        existing.update(
            {
                "term": term_text,
                "parent_id": parent_id,
                "status": status,
                "scope_note": scope_note.strip(),
                "history_note": history_note.strip(),
                "editorial_comment": editorial_comment.strip(),
                "related_ids": clean_related,
                "updated_at": timestamp,
                "updated_by": user_name.strip(),
            }
        )
        return existing["id"]

    new_id = str(uuid.uuid4())
    data.setdefault("terms", []).append(
        {
            "id": new_id,
            "term": term_text,
            "parent_id": parent_id,
            "status": status,
            "scope_note": scope_note.strip(),
            "history_note": history_note.strip(),
            "editorial_comment": editorial_comment.strip(),
            "related_ids": clean_related,
            "comments": [],
            "created_at": timestamp,
            "created_by": user_name.strip(),
            "updated_at": timestamp,
            "updated_by": user_name.strip(),
        }
    )
    return new_id


def delete_term(data: dict[str, Any], term_id: str) -> None:
    if children_of(data, term_id):
        raise ValueError("Não é possível excluir um termo que possui termos filhos.")

    data["terms"] = [term for term in data.get("terms", []) if term.get("id") != term_id]
    for term in data.get("terms", []):
        if term.get("parent_id") == term_id:
            term["parent_id"] = None
        term["related_ids"] = [rid for rid in term.get("related_ids", []) if rid != term_id]


def add_comment(data: dict[str, Any], term_id: str, author: str, text: str) -> None:
    term = get_term(data, term_id)
    if not term:
        raise ValueError("Termo não encontrado.")
    text = text.strip()
    if not text:
        raise ValueError("Digite o comentário.")
    term.setdefault("comments", []).append(
        {
            "id": str(uuid.uuid4()),
            "author": author.strip() or "Sem identificação",
            "text": text,
            "created_at": now_iso(),
        }
    )
    term["updated_at"] = now_iso()
    term["updated_by"] = author.strip()


def path_for_term(data: dict[str, Any], term_id: str) -> str:
    parts = []
    current = term_id
    seen: set[str] = set()
    while current and current not in seen:
        seen.add(current)
        term = get_term(data, current)
        if not term:
            break
        parts.append(term_label(term))
        current = term.get("parent_id")
    return " > ".join(reversed(parts))


# -----------------------------------------------------------------------------
# Exportação/importação
# -----------------------------------------------------------------------------


def data_to_csv(data: dict[str, Any]) -> str:
    output = io.StringIO()
    fieldnames = [
        "termo",
        "termo_pai",
        "situacao",
        "nota_explicativa",
        "nota_historica",
        "comentario_editorial",
        "termos_relacionados",
        "criado_em",
        "atualizado_em",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()

    for term in sorted_terms(data):
        related_names = [term_name_by_id(data, rid) for rid in term.get("related_ids", [])]
        writer.writerow(
            {
                "termo": term.get("term", ""),
                "termo_pai": term_name_by_id(data, term.get("parent_id")),
                "situacao": term.get("status", "Ativo"),
                "nota_explicativa": term.get("scope_note", ""),
                "nota_historica": term.get("history_note", ""),
                "comentario_editorial": term.get("editorial_comment", ""),
                "termos_relacionados": "; ".join([name for name in related_names if name]),
                "criado_em": term.get("created_at", ""),
                "atualizado_em": term.get("updated_at", ""),
            }
        )

    return output.getvalue()


def import_csv_into_data(data: dict[str, Any], csv_text: str, user_name: str) -> int:
    reader = csv.DictReader(io.StringIO(csv_text))
    rows = list(reader)
    if not rows:
        return 0

    required = {"termo"}
    if not required.issubset(set(reader.fieldnames or [])):
        raise ValueError("O CSV precisa ter pelo menos a coluna 'termo'.")

    # Primeiro cria/atualiza os termos, sem hierarquia.
    name_to_id: dict[str, str] = {
        normalize_text(term.get("term", "")).casefold(): term.get("id", "") for term in data.get("terms", [])
    }

    imported = 0
    for row in rows:
        term_text = normalize_text(row.get("termo", ""))
        if not term_text:
            continue
        key = term_text.casefold()
        existing_id = name_to_id.get(key)
        new_id = upsert_term(
            data,
            term_id=existing_id,
            term_text=term_text,
            parent_id=None,
            status=row.get("situacao") or "Ativo",
            scope_note=row.get("nota_explicativa") or row.get("nota_escopo") or "",
            history_note=row.get("nota_historica") or "",
            editorial_comment=row.get("comentario_editorial") or "",
            related_ids=[],
            user_name=user_name,
        )
        name_to_id[key] = new_id
        imported += 1

    # Depois resolve pais e relacionados.
    for row in rows:
        term_text = normalize_text(row.get("termo", ""))
        if not term_text:
            continue
        term_id = name_to_id.get(term_text.casefold())
        term = get_term(data, term_id)
        if not term:
            continue

        parent_name = normalize_text(row.get("termo_pai", ""))
        parent_id = name_to_id.get(parent_name.casefold()) if parent_name else None
        if parent_id and parent_id != term_id and not is_descendant(data, parent_id, term_id):
            term["parent_id"] = parent_id

        related_raw = row.get("termos_relacionados", "") or ""
        related_ids = []
        for piece in related_raw.split(";"):
            related_name = normalize_text(piece)
            rid = name_to_id.get(related_name.casefold())
            if rid and rid != term_id and rid not in related_ids:
                related_ids.append(rid)
        term["related_ids"] = related_ids
        term["updated_at"] = now_iso()
        term["updated_by"] = user_name.strip()

    return imported


# -----------------------------------------------------------------------------
# Interface Streamlit
# -----------------------------------------------------------------------------


def render_tree(data: dict[str, Any], parent_id: str | None = None, level: int = 0) -> None:
    for term in children_of(data, parent_id):
        prefix = "&nbsp;" * (level * 4)
        status = html.escape(term.get("status", "Ativo"))
        st.markdown(f"{prefix}- **{html.escape(term_label(term))}** <small>({status})</small>", unsafe_allow_html=True)
        render_tree(data, term.get("id"), level + 1)


def render_term_card(data: dict[str, Any], term: dict[str, Any]) -> None:
    st.markdown(f"### {html.escape(term_label(term))}")
    st.caption(path_for_term(data, term.get("id")))

    cols = st.columns(3)
    cols[0].metric("Situação", term.get("status", "Ativo"))
    cols[1].metric("Filhos", str(len(children_of(data, term.get("id")))))
    cols[2].metric("Comentários", str(len(term.get("comments", []))))

    parent_name = term_name_by_id(data, term.get("parent_id")) or "—"
    related = [term_name_by_id(data, rid) for rid in term.get("related_ids", [])]
    related = [name for name in related if name]

    st.markdown(f"**Termo pai:** {html.escape(parent_name)}")
    st.markdown(f"**Termos relacionados:** {html.escape('; '.join(related) if related else '—')}")

    if term.get("scope_note"):
        st.markdown("**Nota explicativa / nota de escopo**")
        st.write(term.get("scope_note"))
    if term.get("history_note"):
        st.markdown("**Nota histórica**")
        st.write(term.get("history_note"))
    if term.get("editorial_comment"):
        st.markdown("**Comentário editorial**")
        st.write(term.get("editorial_comment"))

    if term.get("comments"):
        st.markdown("**Comentários**")
        for comment in term.get("comments", []):
            st.markdown(
                f"- {html.escape(comment.get('text', ''))}  \n"
                f"  <small>{html.escape(comment.get('author', ''))} — {html.escape(comment.get('created_at', ''))}</small>",
                unsafe_allow_html=True,
            )


def main() -> None:
    st.set_page_config(page_title=APP_TITLE, page_icon="📚", layout="wide")
    st.title("📚 Vocabulário controlado")
    st.caption("Cadastro colaborativo de termos, hierarquias, notas e comentários — com dados salvos no GitHub.")

    cfg = ensure_github_config_or_stop()

    with st.sidebar:
        st.header("Identificação")
        user_name = st.text_input("Seu nome", value="Equipe", help="Será gravado no histórico dos termos e comentários.")
        st.divider()
        st.caption("Repositório")
        st.code(cfg["repo"])
        st.caption("Arquivo de dados")
        st.code(cfg["path"])
        if st.button("Recarregar dados"):
            st.cache_data.clear()
            st.rerun()

    try:
        data, sha = load_data_from_github(cfg)
    except Exception as exc:
        st.error(str(exc))
        st.stop()

    total = len(data.get("terms", []))
    root_count = len(children_of(data, None))
    st.info(f"Base carregada do GitHub: **{total} termo(s)**, com **{root_count} termo(s) raiz**.")

    tab_register, tab_tree, tab_search, tab_comments, tab_import, tab_help = st.tabs(
        ["Cadastrar / editar", "Hierarquia", "Consultar", "Comentários", "Importar / exportar", "Ajuda"]
    )

    with tab_register:
        st.subheader("Cadastrar ou editar termo")

        term_options = {"+ Novo termo": None}
        term_options.update(label_to_id_map(data, include_empty=False))
        chosen_label = st.selectbox("O que deseja editar?", list(term_options.keys()))
        editing_id = term_options[chosen_label]
        editing_term = get_term(data, editing_id)

        available_parent_options = label_to_id_map(data, include_empty=True, exclude_id=editing_id)
        if editing_id:
            # Remove descendentes da lista de possíveis pais para evitar ciclos.
            available_parent_options = {
                label: tid
                for label, tid in available_parent_options.items()
                if tid is None or not is_descendant(data, tid, editing_id)
            }

        current_parent_id = editing_term.get("parent_id") if editing_term else None
        parent_labels = list(available_parent_options.keys())
        parent_index = 0
        for idx, label in enumerate(parent_labels):
            if available_parent_options[label] == current_parent_id:
                parent_index = idx
                break

        related_options = label_to_id_map(data, include_empty=False, exclude_id=editing_id)
        current_related_ids = set(editing_term.get("related_ids", [])) if editing_term else set()
        current_related_labels = [label for label, tid in related_options.items() if tid in current_related_ids]

        default_status = editing_term.get("status", "Ativo") if editing_term else "Ativo"
        status_index = STATUS.index(default_status) if default_status in STATUS else 0

        with st.form("term_form"):
            term_text = st.text_input("Termo *", value=editing_term.get("term", "") if editing_term else "")
            parent_label = st.selectbox("Termo pai", parent_labels, index=parent_index)
            status = st.selectbox("Situação", STATUS, index=status_index)
            scope_note = st.text_area(
                "Nota explicativa / nota de escopo",
                value=editing_term.get("scope_note", "") if editing_term else "",
                height=100,
            )
            history_note = st.text_area(
                "Nota histórica",
                value=editing_term.get("history_note", "") if editing_term else "",
                height=100,
            )
            editorial_comment = st.text_area(
                "Comentário editorial",
                value=editing_term.get("editorial_comment", "") if editing_term else "",
                height=100,
            )
            related_labels = st.multiselect(
                "Termos relacionados",
                list(related_options.keys()),
                default=current_related_labels,
            )

            col_save, col_delete = st.columns([2, 1])
            save_clicked = col_save.form_submit_button("Salvar termo", type="primary")
            delete_clicked = col_delete.form_submit_button("Excluir termo") if editing_id else False

        if save_clicked:
            try:
                fresh_data, fresh_sha = load_data_from_github(cfg)
                upsert_term(
                    fresh_data,
                    term_id=editing_id,
                    term_text=term_text,
                    parent_id=available_parent_options[parent_label],
                    status=status,
                    scope_note=scope_note,
                    history_note=history_note,
                    editorial_comment=editorial_comment,
                    related_ids=[related_options[label] for label in related_labels if related_options[label]],
                    user_name=user_name,
                )
                save_data_to_github(
                    cfg,
                    fresh_data,
                    fresh_sha,
                    f"Atualiza vocabulário: {normalize_text(term_text) or 'termo sem nome'}",
                )
                st.success("Termo salvo no GitHub.")
                st.rerun()
            except Exception as exc:
                st.error(str(exc))

        if delete_clicked and editing_id:
            try:
                fresh_data, fresh_sha = load_data_from_github(cfg)
                delete_term(fresh_data, editing_id)
                save_data_to_github(cfg, fresh_data, fresh_sha, f"Remove termo: {editing_term.get('term', '')}")
                st.success("Termo excluído do GitHub.")
                st.rerun()
            except Exception as exc:
                st.error(str(exc))

    with tab_tree:
        st.subheader("Hierarquia dos termos")
        if total == 0:
            st.warning("Ainda não há termos cadastrados.")
        else:
            render_tree(data)

    with tab_search:
        st.subheader("Consultar termos")
        query = normalize_text(st.text_input("Buscar por termo ou nota"))
        status_filter = st.multiselect("Filtrar por situação", STATUS, default=[])

        results = []
        for term in sorted_terms(data):
            haystack = " ".join(
                [
                    term.get("term", ""),
                    term.get("scope_note", ""),
                    term.get("history_note", ""),
                    term.get("editorial_comment", ""),
                    term_name_by_id(data, term.get("parent_id")),
                ]
            ).casefold()
            if query and query.casefold() not in haystack:
                continue
            if status_filter and term.get("status", "Ativo") not in status_filter:
                continue
            results.append(term)

        st.write(f"Resultado: **{len(results)} termo(s)**")
        for term in results:
            with st.expander(term_label_with_status(term), expanded=False):
                render_term_card(data, term)

    with tab_comments:
        st.subheader("Adicionar comentário")
        if total == 0:
            st.warning("Cadastre um termo antes de comentar.")
        else:
            comment_options = label_to_id_map(data, include_empty=False)
            selected_comment_label = st.selectbox("Termo", list(comment_options.keys()), key="comment_term")
            selected_term_id = comment_options[selected_comment_label]
            selected_term = get_term(data, selected_term_id)

            if selected_term:
                with st.form("comment_form", clear_on_submit=True):
                    comment_text = st.text_area("Comentário", height=120)
                    add_comment_clicked = st.form_submit_button("Salvar comentário", type="primary")

                if add_comment_clicked:
                    try:
                        fresh_data, fresh_sha = load_data_from_github(cfg)
                        add_comment(fresh_data, selected_term_id, user_name, comment_text)
                        save_data_to_github(
                            cfg,
                            fresh_data,
                            fresh_sha,
                            f"Adiciona comentário: {selected_term.get('term', '')}",
                        )
                        st.success("Comentário salvo no GitHub.")
                        st.rerun()
                    except Exception as exc:
                        st.error(str(exc))

                st.divider()
                render_term_card(data, selected_term)

    with tab_import:
        st.subheader("Importar / exportar")

        st.markdown("**Exportar**")
        json_data = json.dumps(data, ensure_ascii=False, indent=2)
        st.download_button(
            "Baixar JSON",
            data=json_data.encode("utf-8"),
            file_name="vocabulario.json",
            mime="application/json",
        )
        st.download_button(
            "Baixar CSV",
            data=data_to_csv(data).encode("utf-8-sig"),
            file_name="vocabulario.csv",
            mime="text/csv",
        )

        st.divider()
        st.markdown("**Importar CSV**")
        st.caption(
            "Colunas aceitas: termo, termo_pai, situacao, nota_explicativa, nota_historica, "
            "comentario_editorial, termos_relacionados. Em termos_relacionados, separe por ponto e vírgula."
        )
        uploaded = st.file_uploader("Escolha um CSV", type=["csv"])
        if uploaded is not None:
            csv_text = uploaded.getvalue().decode("utf-8-sig")
            if st.button("Importar CSV para o GitHub", type="primary"):
                try:
                    fresh_data, fresh_sha = load_data_from_github(cfg)
                    count = import_csv_into_data(fresh_data, csv_text, user_name)
                    save_data_to_github(cfg, fresh_data, fresh_sha, f"Importa {count} termo(s) por CSV")
                    st.success(f"Importação concluída: {count} termo(s) processado(s).")
                    st.rerun()
                except Exception as exc:
                    st.error(str(exc))

        with st.expander("Modelo de CSV"):
            st.code(
                "termo,termo_pai,situacao,nota_explicativa,nota_historica,comentario_editorial,termos_relacionados\n"
                "Processo legislativo,,Ativo,Conjunto de atos relativos à tramitação de proposições,,,\n"
                "Projeto de lei,Processo legislativo,Ativo,Espécie de proposição legislativa,,,\n"
                "Emenda,Processo legislativo,Ativo,Proposição acessória apresentada a outra proposição,,,Projeto de lei\n",
                language="csv",
            )

    with tab_help:
        st.subheader("Como usar")
        st.markdown(
            """
            1. Cadastre primeiro os termos mais gerais, sem termo pai.
            2. Depois cadastre os termos específicos, escolhendo o termo pai.
            3. Use a nota explicativa para definir o uso do termo.
            4. Use a nota histórica para registrar alterações, origem ou mudanças de denominação.
            5. Use o comentário editorial para observações internas da equipe.
            6. Os dados são gravados no arquivo JSON indicado em Secrets, dentro do repositório GitHub.

            **Atenção:** este modelo é simples e bom para uso leve por equipe pequena. Se muitas pessoas editarem ao
            mesmo tempo, pode haver conflito de gravação. Nesse caso, recarregue a página e salve novamente.
            """
        )


if __name__ == "__main__":
    main()
