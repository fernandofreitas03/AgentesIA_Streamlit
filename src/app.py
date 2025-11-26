import sys
import os
import streamlit as st

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from src.services.triage_agent import TriageAgent

# safe rerun wrapper
def safe_rerun():
    if hasattr(st, "experimental_rerun"):
        try:
            return st.experimental_rerun()
        except Exception:
            pass
    st.session_state.clear()
    st.stop()

st.set_page_config(page_title="Banco Ágil", page_icon="🤖")
st.title("Banco Ágil")
st.write("Seja Bem-vindo. Aqui você terá eficiência e agilidade na sua vida financeira.")

# init agent
if "agent" not in st.session_state:
    st.session_state.agent = TriageAgent()
    first = st.session_state.agent.start()
    st.session_state.messages = [{"role": "assistant", "content": first}]
    st.session_state.done = False

# render history
for m in st.session_state.messages:
    st.chat_message(m["role"]).write(m["content"])

user_input = st.chat_input("Digite sua mensagem")

def append_and_show(role, text):
    if "messages" not in st.session_state:
        st.session_state.messages = []
    st.session_state.messages.append({"role": role, "content": text})
    st.chat_message(role).write(text)

if user_input:
    append_and_show("user", user_input)
    res = st.session_state.agent.handle_user(user_input)
    assistant_text = res.get("assistant", "Desculpe, ocorreu um erro.")
    append_and_show("assistant", assistant_text)
    if res.get("done"):
        st.session_state.done = True

if st.session_state.get("done", False):
    st.info("Atendimento encerrado. Obrigado por usar o Banco Ágil.")
    if st.button("Iniciar nova triagem"):
        st.session_state.clear()
        safe_rerun()
