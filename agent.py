# agent_core.py
import os
from dotenv import load_dotenv
load_dotenv()
import re
import logging
from typing import Literal
from pydantic import BaseModel, Field

from langchain_groq import ChatGroq
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, RemoveMessage
from langgraph.graph import StateGraph, START, END, MessagesState
from langgraph.checkpoint.redis import RedisSaver
from redis import Redis
# from langchain_redis import RedisSaver 
import os

# Setup Logging
logging.basicConfig(level=logging.INFO)

# ==========================================
# 1. KONFIGURASI
# ==========================================
llm_analis = ChatGroq(model="qwen/qwen3-32b", temperature=0.1)
llm_curhat = ChatGroq(model="qwen/qwen3-32b", temperature=0.6)

# Setup Redis Connection
redis_client = Redis(
    host=os.getenv("REDIS_HOST"),
    port=int(os.getenv("REDIS_PORT")),
    decode_responses=True,
    username=os.getenv("REDIS_USERNAME"),
    password=os.getenv("REDIS_PASSWORD"),
)

checkpointer = RedisSaver(redis_client=redis_client)
checkpointer.setup() 



# ==========================================
# 2. STATE & MODEL
# ==========================================
class ChatState(MessagesState):
    kategori_pesan: str
    summary: str

class DeteksiBahaya(BaseModel):
    status: Literal["aman", "bahaya"] = Field(
        description="Pilih 'bahaya' jika user menunjukkan tanda depresi berat atau ingin menyakiti diri."
    )

# ==========================================
# 3. NODES
# ==========================================

def node_pengecekan(state: ChatState):
    pesan_konteks = state["messages"][-10:]
    teks_konteks = "\n".join([f"{msg.type}: {msg.content}" for msg in pesan_konteks])
    
    router = llm_analis.with_structured_output(DeteksiBahaya)
    keputusan = router.invoke([
        SystemMessage(content=(
    "Analisis apakah obrolan mengarah pada bahaya nyawa (bunuh diri/melukai diri). "
    "Jawab hanya dengan memilih salah satu nilai berikut untuk field 'status': "
    "'aman' atau 'bahaya'. Jangan tambahkan penjelasan."
    )),
        HumanMessage(content=teks_konteks)
    ])
    return {"kategori_pesan": keputusan.status}

def node_alert_profesional(state: ChatState):
    user_terakhir = state["messages"][-1].content
    logging.warning(f"🚨 [SILENT ALERT] Potensi bahaya! Pesan: {user_terakhir}")
    return {} 

def node_curhat(state: ChatState):
    # Prompt sistem dasar
    teks_sistem = (
        """
Anda adalah seorang teman curhat yang sangat empatik dan pendengar yang baik.
                ATURAN MUTLAK:
                1. Validasi perasaan pengguna dan berikan respons yang hangat.
                2. JANGAN PERNAH menghakimi atau menyalahkan pengguna.
                3. JANGAN memberikan diagnosis medis atau psikiatris.
                4. JANGAN mendukung atau membenarkan keputusan yang merugikan diri sendiri atau orang lain.
                5. Gunakan bahasa Indonesia yang santai namun sopan, selayaknya teman yang peduli.

                6. IKUTI GAYA BAHASA USER:
                   - Jika user menggunakan "aku-kamu", gunakan juga "aku-kamu".
                   - Jika user menggunakan "gue-lu", Anda boleh menyesuaikan dengan "gue-lu" secara natural.
                   - Jika user menggunakan bahasa formal, balas dengan bahasa formal.
                   - Jangan memaksakan gaya, harus terasa alami.
                   - Tetap jaga kesopanan, jangan ikut bahasa kasar, menghina, atau toxic.

                CONTOH:
                User: "Gue capek banget hari ini"
                AI: "Kedengarannya lo lagi capek banget ya hari ini..."

                User: "Aku sedih banget"
                AI: "Aku bisa ngerti kenapa kamu merasa sedih..."
"""
    )
    
    # Injeksi Summary jika ada
    if state.get("summary"):
        teks_sistem += f"\n\n[INFO - Rangkuman masa lalu]:\n{state['summary']}"
    
    messages_untuk_ai = [SystemMessage(content=teks_sistem)] + state["messages"]
    
    hasil = llm_curhat.invoke(messages_untuk_ai)
    
    # Bersihkan tag think (jika ada)
    hasil.content = re.sub(r'<think>.*?</think>', '', hasil.content, flags=re.DOTALL).strip()

    return {"messages": [AIMessage(content=hasil.content)]}

def node_summarize(state: ChatState):
    pesan = state["messages"]
    summary_lama = state.get("summary", "")
    
    batas_buffer = 5 
    pesan_untuk_dirangkum = pesan[:-batas_buffer] 
    
    prompt_summary = (
        f"Rangkuman sebelumnya: {summary_lama}\n\n"
        "Tambahkan intisari dari pesan berikut ke rangkuman:\n"
        + "\n".join([f"{m.type}: {m.content}" for m in pesan_untuk_dirangkum])
    )
    
    hasil_summary = llm_analis.invoke([HumanMessage(content=prompt_summary)])
    
    delete_actions = [RemoveMessage(id=m.id) for m in pesan_untuk_dirangkum]
    
    return {
        "summary": hasil_summary.content, 
        "messages": delete_actions
    }

# ==========================================
# 4. GRAPH CONSTRUCTION
# ==========================================
def rute_keamanan(state: ChatState):
    return "node_alert_profesional" if state["kategori_pesan"] == "bahaya" else "node_curhat"

def rute_memory(state: ChatState):
    return "node_summarize" if len(state["messages"]) > 20 else END

builder = StateGraph(ChatState)

builder.add_node("node_pengecekan", node_pengecekan)
builder.add_node("node_alert_profesional", node_alert_profesional)
builder.add_node("node_curhat", node_curhat)
builder.add_node("node_summarize", node_summarize)

builder.add_edge(START, "node_pengecekan")
builder.add_conditional_edges("node_pengecekan", rute_keamanan)
builder.add_edge("node_alert_profesional", "node_curhat") 
builder.add_conditional_edges("node_curhat", rute_memory)
builder.add_edge("node_summarize", END)

app_langgraph = builder.compile(checkpointer=checkpointer)

def run_agent(messages, thread_id: str):
    config = {"configurable": {"thread_id": thread_id}}
    return app_langgraph.invoke({"messages": messages}, config=config)


def delete_thread_history(thread_id: str):
    # Cari semua key di Redis yang mengandung ID sesi tersebut
    keys_to_delete = list(redis_client.scan_iter(match=f"*{thread_id}*"))
    
    if not keys_to_delete:
        # Lempar error jika thread_id tidak ditemukan
        raise ValueError(f"Data history tidak ditemukan untuk thread_id: {thread_id}")
    
    try:
        # Hapus semua keys yang ditemukan
        redis_client.delete(*keys_to_delete)
    except Exception as e:
        # Lempar error jika terjadi masalah koneksi/internal Redis
        raise RuntimeError(f"Gagal menghapus history di Redis: {str(e)}")

# for i in range(1, 10):
#     pesan = input(f"Masukkan pesan user untuk sesi {i}: ")
#     hasil = run_agent([HumanMessage(content=pesan)], thread_id=f"sesi_1")
#     print(f"Output AI untuk sesi {i}: {hasil['messages'][-1].content if hasil['messages'] else 'Tidak ada respon'}\n")

# def get_chat_history(thread_id: str):
#     config = {"configurable": {"thread_id": thread_id}}
    
#     # Mengambil state terbaru dari thread_id tersebut
#     state = app_langgraph.get_state(config)
    
#     # Cek apakah ada data di thread tersebut
#     if state.values and "messages" in state.values:
#         messages = state.values["messages"]
#         print(f"--- History untuk {thread_id} ---")
#         for msg in messages:
#             role = "User" if msg.type == "human" else "AI"
#             print(f"{role}: {msg.content}")
#         return messages
#     else:
#         print(f"Tidak ada history untuk thread: {thread_id}")
#         return []

# # Contoh penggunaan:
# history_sesi_1 = get_chat_history("sesi_1")