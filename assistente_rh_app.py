import streamlit as st
from openai import OpenAI
import os, json, io, logging
from datetime import datetime
import pandas as pd
import fitz                             # PyMuPDF
import gspread
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload
import time
import openai

# ----------------------------------------------------------------------
# CONFIGURAÇÕES GERAIS
# ----------------------------------------------------------------------
st.set_page_config(page_title="Assistente Virtual de Recrutamento", page_icon="🤖")

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

client = OpenAI(api_key=st.secrets["OPENAI_API_KEY"])

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]
creds = service_account.Credentials.from_service_account_info(
    json.loads(st.secrets["GOOGLE_SERVICE_ACCOUNT_JSON"]),
    scopes=SCOPES
)
gc = gspread.authorize(creds)
sheet = gc.open("chat_logs_rh").sheet1
drive_service = build("drive", "v3", credentials=creds)

# Alterado para a nova pasta do Drive que você passou
FOLDER_ID = "1cq6KIiN1p-v1ZMqBJbUNk_-t1v3rJ3dDeKXlWRndzIc"

# ----------------------------------------------------------------------
# FUNÇÕES UTILITÁRIAS
# ----------------------------------------------------------------------
@st.cache_data(show_spinner=False)
def extrair_texto_pdf(file_bytes: bytes) -> str:
    texto = ""
    with fitz.open(stream=file_bytes, filetype="pdf") as doc:
        for pagina in doc:
            texto += pagina.get_text()
    return texto

def listar_curriculos_drive():
    res = drive_service.files().list(
        q=f"'{FOLDER_ID}' in parents and mimeType='application/pdf'",
        fields="files(id, name)"
    ).execute()
    return res.get("files", [])

def baixar_curriculo(file_id: str) -> bytes:
    request = drive_service.files().get_media(fileId=file_id)
    file_data = io.BytesIO()
    downloader = MediaIoBaseDownload(file_data, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    file_data.seek(0)
    return file_data.read()

def ler_curriculo_drive(file_id: str, nome: str):
    pdf_bytes = baixar_curriculo(file_id)
    texto = extrair_texto_pdf(pdf_bytes)
    st.session_state.texto_curriculos += f"\n\n===== {nome} =====\n{texto}"

def upload_curriculo(file_uploaded):
    meta = {"name": file_uploaded.name, "parents": [FOLDER_ID]}
    media = MediaIoBaseUpload(file_uploaded, mimetype="application/pdf")
    uploaded = drive_service.files().create(
        body=meta, media_body=media, fields="id, webViewLink"
    ).execute()
    st.success(
        f"Currículo **{file_uploaded.name}** enviado com sucesso! "
        f"[Abrir no Drive]({uploaded['webViewLink']})"
    )

def atualizar_prompt():
    base_preambulo = (
        "Você é um assistente virtual de RH. Ajude na análise de currículos de múltiplos candidatos, gerando tabelas de aderência, cruzamento com vagas, resumos e sugestões de ocupação."
    )
    complemento = st.session_state.get("custom_preamble_sidebar", "").strip()
    if complemento:
        base_preambulo += f"\n\nInstrução personalizada do usuário: {complemento}"
    base_preambulo += (
        f"\n\nInformações dos currículos analisados:\n{st.session_state.texto_curriculos}\n\n"
        f"As vagas disponíveis são:\n{st.session_state.texto_vagas}"
    )
    st.session_state.mensagens[0]["content"] = base_preambulo

def mostrar_historico():
    for msg in st.session_state.mensagens[1:]:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

def registrar_log_acao(usuario_nome, acao, resultado):
    try:
        sheet.append_row([
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            usuario_nome,
            acao,
            resultado[:3000] if resultado else ""  # Google Sheets limita ~50k caracteres por célula
        ])
    except Exception as e:
        logging.error("Erro ao registrar log de ação: %s", e, exc_info=True)

def processar_entrada(prompt_usuario: str):
    atualizar_prompt()
    st.session_state.mensagens.append({"role": "user", "content": prompt_usuario})
    try:
        resposta = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=st.session_state.mensagens
        )
        conteudo = resposta.choices[0].message.content
        st.session_state.mensagens.append({"role": "assistant", "content": conteudo})

        # Registra no log
        registrar_log_acao(
            st.session_state.usuario_nome,
            "Chat",
            f"Pergunta: {prompt_usuario}\nResposta: {conteudo}"
        )
    except Exception as e:
        logging.error("Erro na chamada ao modelo: %s", e, exc_info=True)
        st.session_state.mensagens.append({
            "role": "assistant",
            "content": "Desculpe, ocorreu um erro ao processar sua solicitação."
        })
    st.rerun()

def gerar_tabela_aderencia(curriculos_texto, vagas_texto, modelo_ia):
    prompt = f"""
Você é um assistente de recrutamento. Com base nas vagas abaixo e nos currículos fornecidos, gere uma tabela que mostre a aderência de cada candidato para cada vaga.

- Liste os nomes dos candidatos nas linhas.
- Liste as vagas nas colunas.
- Utilize critérios como: correspondência de competências, experiências, formações e requisitos da vaga.

Apresente os dados em formato de tabela, atribuindo um nível de aderência (ex.: Alto, Médio, Baixo) ou uma pontuação de 0 a 100, se possível.

Currículos analisados:
{curriculos_texto}

Vagas disponíveis:
{vagas_texto}
"""
    tentativas = 5
    for tentativa in range(tentativas):
        try:
            atualizar_prompt()
            resposta = client.chat.completions.create(
                model=modelo_ia,
                messages=[
                    {"role": "system", "content": st.session_state.mensagens[0]["content"]},
                    {"role": "user", "content": prompt}
                ]
            )
            return resposta.choices[0].message.content

        except openai.RateLimitError:
            wait_time = 2 ** tentativa
            st.warning(f"⚠️ Limite atingido. Tentando novamente em {wait_time} segundos...")
            time.sleep(wait_time)
    st.error("❌ Não foi possível gerar a tabela após várias tentativas devido ao limite da API.")
    return "Erro: Limite da API OpenAI atingido."

def gerar_ranking_candidatos(curriculos_texto, vagas_texto, modelo_ia):
    prompt = f"""
Você é um assistente de RH. Com base nos currículos e nas vagas, gere um ranking dos candidatos para cada vaga, apresentando a ordem do mais ao menos aderente e uma breve justificativa.
Currículos analisados:
{curriculos_texto}

Vagas disponíveis:
{vagas_texto}
"""
    atualizar_prompt()
    resposta = client.chat.completions.create(
        model=modelo_ia,
        messages=[{"role": "system", "content": st.session_state.mensagens[0]["content"]},
                  {"role": "user", "content": prompt}]
    )
    return resposta.choices[0].message.content

def gerar_analise_competencias(curriculos_texto, vagas_texto, modelo_ia):
    prompt = f"""
Você é um assistente de RH. Para cada vaga, crie uma tabela listando as principais competências requeridas, destacando para cada candidato quais competências estão presentes e quais faltam.
Currículos analisados:
{curriculos_texto}

Vagas disponíveis:
{vagas_texto}
"""
    atualizar_prompt()
    resposta = client.chat.completions.create(
        model=modelo_ia,
        messages=[{"role": "system", "content": st.session_state.mensagens[0]["content"]},
                  {"role": "user", "content": prompt}]
    )
    return resposta.choices[0].message.content

def gerar_resumo_profissional(curriculos_texto, vagas_texto, modelo_ia):
    prompt = f"""
Você é um assistente de RH. Para cada candidato, gere um resumo personalizado destacando seus principais pontos fortes em relação às vagas disponíveis.
Currículos analisados:
{curriculos_texto}

Vagas disponíveis:
{vagas_texto}
"""
    atualizar_prompt()
    resposta = client.chat.completions.create(
        model=modelo_ia,
        messages=[{"role": "system", "content": st.session_state.mensagens[0]["content"]},
                  {"role": "user", "content": prompt}]
    )
    return resposta.choices[0].message.content

def detectar_palavras_chave(curriculos_texto, vagas_texto, modelo_ia):
    prompt = f"""
Você é um assistente de RH. Identifique as palavras-chave técnicas e comportamentais (soft skills) mais recorrentes nos currículos, comparando com as palavras-chave das vagas.
Currículos analisados:
{curriculos_texto}

Vagas disponíveis:
{vagas_texto}
"""
    atualizar_prompt()
    resposta = client.chat.completions.create(
        model=modelo_ia,
        messages=[{"role": "system", "content": st.session_state.mensagens[0]["content"]},
                  {"role": "user", "content": prompt}]
    )
    return resposta.choices[0].message.content

def gerar_perguntas_entrevista(curriculos_texto, vagas_texto, modelo_ia):
    prompt = f"""
Você é um assistente de RH. Para cada candidato, sugira perguntas personalizadas para entrevista, baseando-se em lacunas ou pontos de destaque dos currículos em relação às vagas.
Currículos analisados:
{curriculos_texto}

Vagas disponíveis:
{vagas_texto}
"""
    atualizar_prompt()
    resposta = client.chat.completions.create(
        model=modelo_ia,
        messages=[{"role": "system", "content": st.session_state.mensagens[0]["content"]},
                  {"role": "user", "content": prompt}]
    )
    return resposta.choices[0].message.content

def apontar_riscos_alertas(curriculos_texto, vagas_texto, modelo_ia):
    prompt = f"""
Você é um assistente de RH. Liste possíveis incompatibilidades, como ausência de requisitos obrigatórios, experiência insuficiente ou inconsistências nos currículos em relação às vagas.
Currículos analisados:
{curriculos_texto}

Vagas disponíveis:
{vagas_texto}
"""
    atualizar_prompt()
    resposta = client.chat.completions.create(
        model=modelo_ia,
        messages=[{"role": "system", "content": st.session_state.mensagens[0]["content"]},
                  {"role": "user", "content": prompt}]
    )
    return resposta.choices[0].message.content

def analisar_expectativa_salarial(curriculos_texto, vagas_texto, modelo_ia):
    prompt = f"""
Você é um assistente de RH. (Se a informação existir nos currículos) Apresente e compare as expectativas salariais dos candidatos com o orçamento das vagas.
Currículos analisados:
{curriculos_texto}

Vagas disponíveis:
{vagas_texto}
"""
    atualizar_prompt()
    resposta = client.chat.completions.create(
        model=modelo_ia,
        messages=[{"role": "system", "content": st.session_state.mensagens[0]["content"]},
                  {"role": "user", "content": prompt}]
    )
    return resposta.choices[0].message.content

def analisar_diversidade(curriculos_texto, vagas_texto, modelo_ia):
    prompt = f"""
Você é um assistente de RH. (Se disponível nos dados dos currículos) Gera indicadores de diversidade de gênero, idade e formação dos candidatos.
Currículos analisados:
{curriculos_texto}

Vagas disponíveis:
{vagas_texto}
"""
    atualizar_prompt()
    resposta = client.chat.completions.create(
        model=modelo_ia,
        messages=[{"role": "system", "content": st.session_state.mensagens[0]["content"]},
                  {"role": "user", "content": prompt}]
    )
    return resposta.choices[0].message.content

# ----------------------------------------------------------------------
# ESTADO INICIAL
# ----------------------------------------------------------------------
st.session_state.setdefault("texto_curriculos", "")
st.session_state.setdefault("texto_vagas", "")
st.session_state.setdefault("sugestoes_exibidas", False)
if "mensagens" not in st.session_state:
    st.session_state.mensagens = [{"role": "system", "content": ""}]
    atualizar_prompt()

# ----------------------------------------------------------------------
# SIDEBAR ORGANIZADA
# ----------------------------------------------------------------------
with st.sidebar:
    st.image("logo_unesp.png", width=200)
    st.markdown(
        "<div style='font-size:18px; font-weight:bold; margin-bottom: 12px;'>Prof. Dra. Claudia Regina de Freitas</div>",
        unsafe_allow_html=True
    )
    usuario_nome = st.text_input("Digite seu nome completo:", key="nome_usuario_input_sidebar")
    if not usuario_nome:
        st.warning("Por favor, preencha seu nome para iniciar.")
        st.stop()
    st.session_state.usuario_nome = usuario_nome

    st.subheader("📝 Personalize o Assistente")
    custom_preamble = st.text_area(
        "Complemento opcional ao preâmbulo do assistente (ex: priorize experiência em projetos, foco em inglês fluente, etc):",
        key="custom_preamble_sidebar"
    )
    st.caption("⚡ O texto personalizado será considerado automaticamente na próxima análise ou mensagem enviada, sem necessidade de recarregar a página.")

    st.subheader("📑 Vagas disponíveis (CSV local)")
    try:
        vagas_df = pd.read_csv("vagas_exemplo.csv")
        st.dataframe(vagas_df)
        st.session_state.texto_vagas = vagas_df.to_string(index=False)
    except Exception:
        st.warning("Arquivo de vagas não encontrado.")
        st.session_state.texto_vagas = ""

    st.subheader("📄 Selecionar currículos para análise")
    curriculos = listar_curriculos_drive()
    nomes = [c["name"] for c in curriculos]
    selecionados = st.multiselect("Selecione currículos:", nomes, key="multiselect_curriculos_sidebar")
    col_le, col_to = st.columns(2)
    with col_le:
        if st.button("🔍 Ler selecionados", key="botao_ler_selecionados_sidebar"):
            if not selecionados:
                st.warning("Selecione pelo menos um currículo.")
            else:
                for nome in selecionados:
                    file_id = next(c["id"] for c in curriculos if c["name"] == nome)
                    ler_curriculo_drive(file_id, nome)
                atualizar_prompt()
                st.success("Currículos lidos e armazenados na memória!")
    with col_to:
        if st.button("📥 Ler TODOS", key="botao_ler_todos_sidebar"):
            for c in curriculos:
                ler_curriculo_drive(c["id"], c["name"])
            atualizar_prompt()
            st.success("Todos os currículos lidos!")

    st.subheader("📤 Enviar novo currículo (PDF) para o Google Drive")
    file_uploaded = st.file_uploader("Selecione o arquivo", type=["pdf"], key="upload_curriculo_sidebar")
    if file_uploaded and st.button("🚀 Enviar", key="enviar_curriculo_sidebar"):
        upload_curriculo(file_uploaded)

    st.header("Configurações Avançadas")
    modelo_ia = st.selectbox(
        "Escolha o modelo de IA para análise:",
        options=["gpt-4", "gpt-3.5-turbo"],
        index=1,
        key="selecao_modelo_sidebar"
    )

# ----------------------------------------------------------------------
# PAINEL PRINCIPAL
# ----------------------------------------------------------------------
st.title("Assistente Virtual de Recrutamento")

st.divider()
mostrar_historico()
st.divider()

# ---- Tabela de Aderência (botão rápido) ----
st.subheader("📊 Análise de Aderência Currículo vs Vagas")
if st.button("🔍 Gerar Tabela de Aderência", key="botao_aderencia_principal"):
    if not st.session_state.texto_curriculos or not st.session_state.texto_vagas:
        st.warning("Por favor, carregue currículos e vagas antes de gerar a análise.")
    else:
        with st.spinner("Analisando currículos e vagas..."):
            tabela = gerar_tabela_aderencia(
                st.session_state.texto_curriculos,
                st.session_state.texto_vagas,
                modelo_ia
            )
            st.subheader("🔍 Resultado da Análise de Aderência")
            st.markdown(tabela)
            registrar_log_acao(
                st.session_state.usuario_nome,
                "Análise de Aderência",
                tabela
            )

# ---- Análises Avançadas: Dropdown e botão de executar ----
st.subheader("📈 Análises Avançadas")

analises_disponiveis = {
    "Ranking dos Candidatos": gerar_ranking_candidatos,
    "Análise de Competências": gerar_analise_competencias,
    "Resumo Profissional": gerar_resumo_profissional,
    "Palavras-chave/Soft Skills": detectar_palavras_chave,
    "Perguntas para Entrevista": gerar_perguntas_entrevista,
    "Riscos/Alertas de Incompatibilidade": apontar_riscos_alertas,
    "Expectativa Salarial": analisar_expectativa_salarial,
    "Diversidade": analisar_diversidade,
}

analise_escolhida = st.selectbox(
    "Selecione o tipo de análise:",
    options=list(analises_disponiveis.keys()),
    key="analise_avancada_selectbox"
)

if st.button("Executar Análise Avançada", key="botao_analise_avancada"):
    if not st.session_state.texto_curriculos or not st.session_state.texto_vagas:
        st.warning("Por favor, carregue currículos e vagas antes de gerar a análise.")
    else:
        with st.spinner(f"Executando análise: {analise_escolhida}..."):
            resultado = analises_disponiveis[analise_escolhida](
                st.session_state.texto_curriculos,
                st.session_state.texto_vagas,
                modelo_ia
            )
            st.subheader(f"🔍 Resultado da Análise: {analise_escolhida}")
            st.markdown(resultado)
            registrar_log_acao(
                st.session_state.usuario_nome,
                f"Análise Avançada: {analise_escolhida}",
                resultado
            )

# ---- Campo de entrada do usuário (chat) ----
prompt_usuario = st.chat_input("Digite sua mensagem para o assistente...")
if prompt_usuario:
    processar_entrada(prompt_usuario)
