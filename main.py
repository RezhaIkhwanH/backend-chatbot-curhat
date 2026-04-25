import os
from datetime import datetime, timedelta
from typing import Optional

from fastapi import FastAPI, HTTPException, Depends, status,Request 
from fastapi.responses import JSONResponse
from fastapi.security import OAuth2PasswordBearer
from fastapi.middleware.cors import CORSMiddleware
from jose import JWTError, jwt
from passlib.context import CryptContext
from postgrest import APIError
from pydantic import BaseModel, EmailStr
from supabase import create_client, Client
from dotenv import load_dotenv
from agent import run_agent ,delete_thread_history, app_langgraph

load_dotenv()

# --- KONFIGURASI ---
SECRET_KEY = os.getenv("SECRET_KEY")
ALGORITHM = os.getenv("ALGORITHM")
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES"))

supabase: Client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")

app = FastAPI(title="Chatbot AI sikolog Backend", version="1.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  
    allow_credentials=True,
    allow_methods=["*"],  
    allow_headers=["*"],
)


# --- GLOBAL EXCEPTION HANDLER (Standar Response Error) ---
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"success": False, "message": exc.detail, "data": {}, "codeStatus": exc.status_code}
    )

@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={"success": False, "message": "Terjadi kesalahan pada server", "data": {}, "codeStatus": 500}
    )

# --- STANDAR RESPONSE HELPER ---
def format_response(success: bool, message: str, data: any = None, codeStatus: int = 200):
    return {
        "success": success,
        "message": message,
        "data": data if data is not None else {},
        "codeStatus": codeStatus
    }

# --- SCHEMAS ---
class UserRegister(BaseModel):
    username: str
    email: EmailStr
    password: str

class UserLogin(BaseModel):
    email: EmailStr
    password: str

class Token(BaseModel):
    access_token: str
    token_type: str

class UserUpdate(BaseModel):
    username: Optional[str] = None
    email: Optional[EmailStr] = None
    password: Optional[str] = None

class RoomRequest(BaseModel):
    name: str

class ChatRequest(BaseModel):
    message: str
    id_room: int



# --- HELPER FUNCTIONS ---

def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)

def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        return pwd_context.verify(plain_password, hashed_password)
    except Exception:
        return False

def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

# --- NEW HELPER: TOKEN VERIFICATION DEPENDENCY ---

async def get_current_user(token: str = Depends(oauth2_scheme)):
    """
    memvalidasi token dan mengambil data user.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Token kedaluwarsa atau tidak valid",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        if email is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    try:
        # Langsung ambil data user dari DB
        response = supabase.table("users").select("*").eq("email", email).single().execute()
        return response.data
    except APIError as e:
        if e.code == "PGRST116":
            raise HTTPException(status_code=404, detail="User tidak ditemukan")
        # raise HTTPException(status_code=500, detail="Database Error")
        raise HTTPException(status_code=500, detail=e.message if hasattr(e, 'message') else str(e))

# --- ENDPOINTS ---

# --- AUTH ---

@app.post("/register", status_code=status.HTTP_201_CREATED, tags=["Auth"])
async def register(user: UserRegister):
    """ endpoint untuk registrasi user baru"""

    existing_user = supabase.table("users").select("id").eq("email", user.email).execute()
    if existing_user.data:
        raise HTTPException(status_code=400, detail="Email sudah terdaftar")
    
    new_user_data = {
        "username": user.username,
        "email": user.email,
        "password": get_password_hash(user.password)
    }
    
    try:
        supabase.table("users").insert(new_user_data).execute()
        return format_response(True, "Registrasi berhasil!", None, 201)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database Error: {str(e)}")


@app.post("/login", tags=["Auth"])
async def login(login_data: UserLogin):
    """ endpoint untuk login hasil token JWT untuk autentikasi"""

    response = supabase.table("users").select("*").eq("email", login_data.email).execute()
    user = response.data[0] if response.data else None

    if not user or not verify_password(login_data.password, user["password"]):
        raise HTTPException(status_code=401, detail="Email atau password salah")

    access_token = create_access_token(data={"sub": user["email"], "username": user["username"]})
    return format_response(True, "Login berhasil", {"access_token": access_token, "token_type": "bearer"})


# --- USER ---

@app.get("/me", tags=["User"])
async def read_users_me(current_user: dict = Depends(get_current_user)):
    """get profile user login."""

    try:
        current_user.pop("password", None)
        return format_response(True, "Profil user", current_user)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Gagal mengambil profil: {str(e)}")


@app.put("/update-profile", tags=["User"])
async def update_user_profile(
    update_data: UserUpdate, 
    current_user: dict = Depends(get_current_user)
):
    """Endpoint untuk update profil user"""

    update_dict = {}
    
    if update_data.username:
        update_dict["username"] = update_data.username

    if update_data.email and update_data.email != current_user["email"]:
        existing = supabase.table("users").select("id").eq("email", update_data.email).execute()
        if existing.data:
            raise HTTPException(status_code=400, detail="Email baru sudah digunakan")
        
        update_dict["email"] = update_data.email

    if update_data.password:
        update_dict["password"] = get_password_hash(update_data.password)

    if not update_dict:
        return format_response(True, "Tidak ada data yang diupdate")

    try:
        data = supabase.table("users").update(update_dict).eq("id", current_user["id"]).execute()
        data.data[0].pop("password", None)
        return format_response(True, "Profil berhasil diperbarui", data.data[0])
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Update gagal: {str(e)}")


@app.delete("/delete-account", tags=["User"])
async def delete_account(current_user: dict = Depends(get_current_user)):
    """Endpoint untuk menghapus akun"""
    try:
        supabase.table("users").delete().eq("id", current_user["id"]).execute()
        return format_response(True,"akun berhasil di hapus")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Delete gagal: {str(e)}")
    

# --- CHAT ---

@app.get("/chat-history/{room_id}", tags=["Chat"])
async def get_chat_history(
    room_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Endpoint untuk mendapatkan history chat berdasarkan room_id."""

    try:

        data_room = (supabase.table("rooms")
                     .select("id").eq("id", room_id)
                     .eq("user_id", current_user["id"])
                     .maybe_single()
                     .execute())
        
        if not data_room:
            raise HTTPException(status_code=500, detail="gagal ambil history: Ruangan tidak ditemukan")
        
        thread_id = f"room_{room_id}_user_{current_user['id']}"
        config = {"configurable": {"thread_id": thread_id}}

        state = app_langgraph.get_state(config)

        if state.values and "messages" in state.values:
            messages = state.values["messages"]

            formatted_messages = []
            for msg in messages:
                formatted_messages.append({
                    "role": "user" if msg.type == "human" else "ai",
                    "content": msg.content
                })

            return format_response(
                True,
                "berhasil mengambil chat history",
                formatted_messages
            )
            

        return format_response(
                True,
                "tidak ada chat history"
            )

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=str(e)
        )


@app.get("/rooms", tags=["Chat"])
async def get_rooms(current_user: dict = Depends(get_current_user)):
    """Endpoint untuk mendapatkan daftar ruangan chat milik user"""
    try:
        response = supabase.table("rooms").select("*").eq("user_id", current_user["id"]).execute()
        return format_response(True, "Daftar ruangan berhasil di ambil", response.data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Gagal mengambil ruangan: {str(e)}")


@app.post("/chat", tags=["Chat"])
async def send_message(chatReq: ChatRequest, current_user: dict = Depends(get_current_user)):
    """
    chat endpoint belom jalan.
    """
    try:
        data_room = (supabase.table("rooms")
                     .select("id")
                     .eq("id", chatReq.id_room)
                     .eq("user_id", current_user["id"])
                     .maybe_single()
                     .execute())
        
        if not data_room:
            raise HTTPException(status_code=500, detail="Ruangan tidak ditemukan")
        
        response_model = run_agent(
            [{"type": "human", "content": chatReq.message}],
            thread_id=f"room_{chatReq.id_room}_user_{current_user['id']}"
        ) 

        return format_response(True, "Respon AI", {"message": response_model["messages"][-1].content})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error saat memproses chat: {str(e)}")


@app.post("/add_room", status_code=status.HTTP_201_CREATED, tags=["Chat"])
async def add_room(room_data: RoomRequest, current_user: dict = Depends(get_current_user)):
    """Endpoint untuk menambahkan ruangan chat"""
    try:
        data = supabase.table("rooms").insert({
            "title": room_data.name,
            "user_id": current_user["id"]
        }).execute()
        return format_response(True, "Ruangan berhasil ditambahkan", data.data[0], 201)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Gagal menambahkan ruangan: {str(e)}")


@app.delete("/delete_room/{room_id}", tags=["Chat"])
async def delete_room(room_id: int, current_user: dict = Depends(get_current_user)):
    """Endpoint untuk menghapus ruangan chat"""
    try:
        supabase.table("rooms").delete().eq("id", room_id).eq("user_id", current_user["id"]).execute()
        delete_thread_history(f"room_{room_id}_user_{current_user['id']}")
        return format_response(True, "Ruangan berhasil dihapus")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Gagal menghapus ruangan: {str(e)}")