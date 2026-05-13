import os
from datetime import datetime, timezone

import pandas as pd
import streamlit as st
from supabase import create_client

# ============================================================
# Configuração básica
# ============================================================
st.set_page_config(
    page_title="Tesauro",
    page_icon="📚",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
    <style>
    .block-container {
        padding-top: 1.5rem;
        padding-bottom: 2rem;
        max-width: 1500px;
    }
    [data-testid="stSidebar"] {
        display: none;
    }
    .main-title {
        font-size: 1.3rem;
        font-weight: 600;
        margin-bottom: .3rem;
    }
    .muted {
        color: #666;
        font-size: .9rem;
    }
    .term-row {
        padding: .18rem .35rem;
        border-radius: .25rem;
        font-size: .95rem;
    }
    .term-selected {
        background: #e8f0fe;
        font-weight: 600;
    }
    .comment-box {
        border: 1px solid #ddd;
        border-radius: .5rem;
        padding: .7rem;
        margin-bottom: .6rem;
        background: #fff;
    }
    .resolved {
        background: #f3f3f3;
        color: #666;
    }
    div[data-testid="stVerticalBlockBorderWrapper"] {
        border-color: #ddd;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# ============================================================
# Conexão com Supabase
# No Streamlit Cloud, configure em Settings > Secrets:
# SUPABASE_URL="..."
# SUPABASE_KEY="..."
# ============================================================
@st.cache_resource
def get_supabase_client():
    url = st.secrets.get("SUPABASE_URL", os.getenv("SUPABASE_URL"))
    key = st.secrets.get("SUPABASE_KEY", os.getenv("SUPABASE_KEY"))
    if not url or not key:
        st.error("Configure SUPABASE_URL e SUPABASE_KEY nos secrets do Streamlit.")
        st.stop()
    return create_client(url, key)

supabase = get_supabase_client()

# ============================================================
# Funções de banco
# ============================================================
@st.cache_data(ttl=10)
def carregar_usuarios():
    resp = supabase.table("usuarios").select("*").eq("ativo", True).order("nome").execute()
    return resp.data or []

@st.cache_data(ttl=10)
def carregar_termos():
    resp = supabase.table("termos").select("*").order("termo").execute()
    return resp.data or []

@st.cache_data(ttl=10)
def carregar_comentarios(termo_id):
    if not termo_id:
        return []
    resp = (
        supabase.table("comentarios")
        .select("*, usuarios:autor_id(nome, email), resolvedor:resolvido_por(nome, email)")
        .eq("termo_id", termo_id)
        .order("resolvido", desc=False)
        .order("criado_em", desc=True)
        .execute()
    )
    return resp.data or []

@st.cache_data(ttl=10)
def carregar_relacoes(termo_id):
    if not termo_id:
        return []
    resp = (
        supabase.table("relacoes_termos")
        .select("*, destino:termo_destino_id(id, termo)")
        .eq("termo_origem_id", termo_id)
        .order("tipo_relacao")
        .execute()
    )
    return resp.data or []

def limpar_cache():
    carregar_usuarios.clear()
    carregar_termos.clear()
    carregar_comentarios.clear()
    carregar_relacoes.clear()

def agora_iso():
    return datetime.now(timezone.utc).isoformat()

def salvar_termo(termo_id, dados):
    dados["atualizado_em"] = agora_iso()
    supabase.table("termos").update(dados).eq("id", termo_id).execute()
    limpar_cache()

def criar_termo(dados):
    dados["criado_em"] = agora_iso()
    dados["atualizado_em"] = agora_iso()
    resp = supabase.table("termos").insert(dados).execute()
    limpar_cache()
    return resp.data[0] if resp.data else None

def criar_comentario(termo_id, autor_id, texto):
    supabase.table("comentarios").insert(
        {
            "termo_id": termo_id,
            "autor_id": autor_id,
            "texto": texto,
            "resolvido": False,
            "criado_em": agora_iso(),
        }
    ).execute()
    limpar_cache()

def resolver_comentario(comentario_id, usuario_id):
    supabase.table("comentarios").update(
        {
            "resolvido": True,
            "resolvido_por": usuario_id,
            "resolvido_em": agora_iso(),
        }
    ).eq("id", comentario_id).execute()
    limpar_cache()

def reabrir_comentario(comentario_id):
    supabase.table("comentarios").update(
        {
            "resolvido": False,
            "resolvido_por": None,
            "resolvido_em": None,
        }
    ).eq("id", comentario_id).execute()
    limpar_cache()

# ============================================================
# Utilidades da árvore
# ============================================================
def montar_filhos(termos):
    filhos = {}
    for t in termos:
        filhos.setdefault(t.get("termo_pai_id"), []).append(t)
    for chave in filhos:
        filhos[chave] = sorted(filhos[chave], key=lambda x: (x.get("termo") or "").lower())
    return filhos

def nivel_termo(termo_id, mapa_por_id):
    nivel = 0
    atual = mapa_por_id.get(termo_id)
    visitados = set()
    while atual and atual.get("termo_pai_id") and atual["termo_pai_id"] not in visitados:
        visitados.add(atual["id"])
        nivel += 1
        atual = mapa_por_id.get(atual.get("termo_pai_id"))
    return nivel

def termo_path(termo_id, mapa_por_id):
    partes = []
    atual = mapa_por_id.get(termo_id)
    visitados = set()
    while atual and atual.get("id") not in visitados:
        visitados.add(atual["id"])
        partes.append(atual.get("termo") or "")
        atual = mapa_por_id.get(atual.get("termo_pai_id"))
    return " / ".join(reversed(partes))

def render_arvore(termos, selecionado_id=None, filtro=""):
    mapa = {t["id"]: t for t in termos}
    filhos = montar_filhos(termos)
    filtro = (filtro or "").strip().lower()

    def corresponde(t):
        if not filtro:
            return True
        texto = " ".join(
            [
                t.get("termo") or "",
                t.get("definicao") or "",
                t.get("nota_escopo") or "",
                t.get("nota_uso") or "",
            ]
        ).lower()
        return filtro in texto

    visiveis = set()
    if filtro:
        for t in termos:
            if corresponde(t):
                atual = t
                while atual:
                    visiveis.add(atual["id"])
                    atual = mapa.get(atual.get("termo_pai_id"))
    else:
        visiveis = {t["id"] for t in termos}

    linhas = []

    def andar(pai_id=None, nivel=0):
        for t in filhos.get(pai_id, []):
            if t["id"] not in visiveis:
                continue
            tem_filhos = bool(filhos.get(t["id"]))
            icone = "▾" if tem_filhos else "•"
            label = f"{'&nbsp;' * (nivel * 4)}{icone} {t['termo']}"
            selecionado = t["id"] == selecionado_id
            linhas.append((t, label, selecionado))
            andar(t["id"], nivel + 1)

    andar(None, 0)
    return linhas

# ============================================================
# Login simples por e-mail autorizado
# Para protótipo. Depois pode trocar por st.login/OIDC.
# ============================================================
usuarios = carregar_usuarios()
usuarios_por_email = {u["email"].lower(): u for u in usuarios}

st.markdown('<div class="main-title">Tesauro</div>', unsafe_allow_html=True)
st.markdown('<div class="muted">Protótipo simples: árvore de termos, ficha lateral e comentários resolvíveis.</div>', unsafe_allow_html=True)

if "usuario_email" not in st.session_state:
    st.session_state.usuario_email = ""

with st.expander("Entrar", expanded=not bool(st.session_state.usuario_email)):
    email_digitado = st.text_input("E-mail autorizado", value=st.session_state.usuario_email, placeholder="nome@instituicao.gov.br")
    if st.button("Entrar", type="primary"):
        email_normalizado = email_digitado.strip().lower()
        if email_normalizado in usuarios_por_email:
            st.session_state.usuario_email = email_normalizado
            st.rerun()
        else:
            st.error("E-mail não autorizado. Cadastre este usuário na tabela usuarios.")

usuario = usuarios_por_email.get(st.session_state.usuario_email.lower()) if st.session_state.usuario_email else None

if not usuario:
    st.info("Informe um e-mail cadastrado para acessar o protótipo.")
    st.stop()

col_user, col_logout = st.columns([8, 1])
with col_user:
    st.caption(f"Usuário: {usuario['nome']} · Perfil: {usuario.get('perfil', 'revisor')}")
with col_logout:
    if st.button("Sair"):
        st.session_state.usuario_email = ""
        st.rerun()

# ============================================================
# Dados principais
# ============================================================
termos = carregar_termos()
mapa_por_id = {t["id"]: t for t in termos}

if "termo_selecionado_id" not in st.session_state:
    st.session_state.termo_selecionado_id = termos[0]["id"] if termos else None

# ============================================================
# Layout principal: árvore à esquerda, ficha à direita
# ============================================================
esq, dir = st.columns([0.36, 0.64], gap="large")

with esq:
    st.subheader("Hierarquia")
    filtro = st.text_input("Pesquisar termo", placeholder="Digite parte do termo, definição ou nota")

    with st.container(border=True):
        linhas = render_arvore(termos, st.session_state.termo_selecionado_id, filtro)
        if not linhas:
            st.caption("Nenhum termo encontrado.")
        for t, label, selecionado in linhas:
            cols = st.columns([1])
            key = f"select_{t['id']}"
            texto_botao = label.replace("&nbsp;", "  ")
            if st.button(texto_botao, key=key, use_container_width=True, type="primary" if selecionado else "secondary"):
                st.session_state.termo_selecionado_id = t["id"]
                st.rerun()

    st.divider()
    st.subheader("Novo termo")
    with st.form("novo_termo"):
        novo_termo = st.text_input("Termo")
        opcoes_pai = {"Sem termo pai": None}
        opcoes_pai.update({termo_path(t["id"], mapa_por_id): t["id"] for t in termos})
        novo_pai_label = st.selectbox("Termo pai", list(opcoes_pai.keys()))
        novo_status = st.selectbox("Status", ["candidato", "em revisão", "aprovado", "publicado", "obsoleto"], index=1)
        criar = st.form_submit_button("Criar termo")
        if criar:
            if not novo_termo.strip():
                st.warning("Informe o termo.")
            else:
                criado = criar_termo(
                    {
                        "termo": novo_termo.strip(),
                        "termo_pai_id": opcoes_pai[novo_pai_label],
                        "status": novo_status,
                        "criado_por": usuario["id"],
                        "atualizado_por": usuario["id"],
                    }
                )
                if criado:
                    st.session_state.termo_selecionado_id = criado["id"]
                st.success("Termo criado.")
                st.rerun()

with dir:
    termo_id = st.session_state.termo_selecionado_id
    termo = mapa_por_id.get(termo_id)

    if not termo:
        st.info("Selecione ou crie um termo.")
        st.stop()

    st.subheader("Descritor selecionado")
    st.caption(termo_path(termo_id, mapa_por_id))

    aba_cadastro, aba_relacoes, aba_comentarios = st.tabs(["Cadastro", "Relacionamentos", "Comentários"])

    with aba_cadastro:
        with st.form("editar_termo"):
            termo_txt = st.text_input("Termo", value=termo.get("termo") or "")
            definicao = st.text_area("Definição", value=termo.get("definicao") or "", height=120)
            nota_escopo = st.text_area("Nota de escopo", value=termo.get("nota_escopo") or "", height=100)
            nota_uso = st.text_area("Nota de uso", value=termo.get("nota_uso") or "", height=80)

            opcoes_pai = {"Sem termo pai": None}
            opcoes_pai.update(
                {
                    termo_path(t["id"], mapa_por_id): t["id"]
                    for t in termos
                    if t["id"] != termo_id
                }
            )
            pai_atual_label = "Sem termo pai"
            for label, valor in opcoes_pai.items():
                if valor == termo.get("termo_pai_id"):
                    pai_atual_label = label
                    break

            pai_label = st.selectbox("Termo geral", list(opcoes_pai.keys()), index=list(opcoes_pai.keys()).index(pai_atual_label))
            status_opcoes = ["candidato", "em revisão", "aprovado", "publicado", "obsoleto"]
            status_atual = termo.get("status") or "em revisão"
            status = st.selectbox("Status", status_opcoes, index=status_opcoes.index(status_atual) if status_atual in status_opcoes else 1)

            salvar = st.form_submit_button("Salvar alterações", type="primary")
            if salvar:
                salvar_termo(
                    termo_id,
                    {
                        "termo": termo_txt.strip(),
                        "definicao": definicao.strip() or None,
                        "nota_escopo": nota_escopo.strip() or None,
                        "nota_uso": nota_uso.strip() or None,
                        "termo_pai_id": opcoes_pai[pai_label],
                        "status": status,
                        "atualizado_por": usuario["id"],
                    },
                )
                st.success("Termo atualizado.")
                st.rerun()

    with aba_relacoes:
        st.markdown("**Termo geral**")
        pai = mapa_por_id.get(termo.get("termo_pai_id"))
        st.write(pai["termo"] if pai else "—")

        st.markdown("**Termos específicos**")
        filhos = [t for t in termos if t.get("termo_pai_id") == termo_id]
        if filhos:
            for f in sorted(filhos, key=lambda x: x["termo"].lower()):
                st.write(f"• {f['termo']}")
        else:
            st.write("—")

        st.markdown("**Outras relações cadastradas**")
        relacoes = carregar_relacoes(termo_id)
        if relacoes:
            df = pd.DataFrame(
                [
                    {
                        "tipo": r.get("tipo_relacao"),
                        "termo": (r.get("destino") or {}).get("termo"),
                    }
                    for r in relacoes
                ]
            )
            st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.caption("Nenhuma relação adicional cadastrada.")

    with aba_comentarios:
        st.markdown("**Novo comentário**")
        with st.form("novo_comentario"):
            texto = st.text_area("Comentário", placeholder="Escreva uma observação sobre este termo...")
            enviar = st.form_submit_button("Adicionar comentário", type="primary")
            if enviar:
                if texto.strip():
                    criar_comentario(termo_id, usuario["id"], texto.strip())
                    st.success("Comentário adicionado.")
                    st.rerun()
                else:
                    st.warning("Digite o comentário.")

        st.divider()
        st.markdown("**Comentários do termo**")
        comentarios = carregar_comentarios(termo_id)
        if not comentarios:
            st.caption("Nenhum comentário para este termo.")
        for c in comentarios:
            autor = c.get("usuarios") or {}
            resolvedor = c.get("resolvedor") or {}
            classe = "comment-box resolved" if c.get("resolvido") else "comment-box"
            st.markdown(
                f"""
                <div class="{classe}">
                    <strong>{autor.get('nome', 'Usuário')}</strong>
                    <span class="muted"> · {c.get('criado_em', '')}</span><br>
                    {c.get('texto', '')}
                    <br><span class="muted">Status: {'resolvido' if c.get('resolvido') else 'aberto'}</span>
                </div>
                """,
                unsafe_allow_html=True,
            )
            cols = st.columns([1, 5])
            with cols[0]:
                if c.get("resolvido"):
                    if st.button("Reabrir", key=f"reabrir_{c['id']}"):
                        reabrir_comentario(c["id"])
                        st.rerun()
                else:
                    if st.button("Resolver", key=f"resolver_{c['id']}"):
                        resolver_comentario(c["id"], usuario["id"])
                        st.rerun()
            with cols[1]:
                if c.get("resolvido"):
                    st.caption(f"Resolvido por {resolvedor.get('nome', 'usuário')} em {c.get('resolvido_em', '')}")

# ============================================================
# Rodapé: exportação simples
# ============================================================
st.divider()
with st.expander("Exportar dados"):
    df_export = pd.DataFrame(termos)
    st.download_button(
        "Baixar termos em CSV",
        data=df_export.to_csv(index=False).encode("utf-8-sig"),
        file_name="termos_tesauro.csv",
        mime="text/csv",
    )
