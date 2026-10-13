from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from datetime import datetime, timezone
from typing import Annotated

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, create_engine, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from starlette.middleware.sessions import SessionMiddleware

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:////data/atlas.db")
SESSION_SECRET = os.getenv("SESSION_SECRET", "dev-only-change-this-session-secret")
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() == "true"
APP_NAME = os.getenv("APP_NAME", "Atlas Metadata")
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
ONE_TIME_TOKENS: dict[str, tuple[int, str, float]] = {}


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(80))
    password_hash: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class ApiToken(Base):
    __tablename__ = "api_tokens"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    label: Mapped[str] = mapped_column(String(80))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    token_prefix: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Game(Base):
    __tablename__ = "games"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    canonical_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(200), index=True)
    developer: Mapped[str] = mapped_column(String(200), default="")
    release_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    description: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class GameCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    developer: str = Field(default="", max_length=200)
    release_year: int | None = Field(default=None, ge=1950, le=2100)
    description: str = Field(default="", max_length=3000)


app = FastAPI(title=APP_NAME, version="0.1.0", description="Prototype catalogue API")
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, same_site="lax", https_only=COOKIE_SECURE)
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


Db = Annotated[Session, Depends(get_db)]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
    return "scrypt$" + salt.hex() + "$" + derived.hex()


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, salt_hex, expected = stored.split("$", 2)
        if algorithm != "scrypt":
            return False
        actual = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1)
        return hmac.compare_digest(actual.hex(), expected)
    except (ValueError, TypeError):
        return False


def new_canonical_id() -> str:
    return "gm_" + secrets.token_hex(6).upper()


def serialize_game(game: Game) -> dict:
    return {"id": game.canonical_id, "title": game.title, "developer": game.developer,
            "release_year": game.release_year, "description": game.description,
            "created_at": game.created_at.isoformat() if game.created_at else None}


def current_user(request: Request, db: Db) -> User | None:
    user_id = request.session.get("user_id")
    return db.get(User, int(user_id)) if user_id else None


def require_api_token(db: Db, authorization: Annotated[str | None, Header()] = None) -> tuple[ApiToken, User]:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Provide a bearer API token")
    raw = authorization[7:].strip()
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    token = db.scalar(select(ApiToken).where(ApiToken.token_hash == digest, ApiToken.revoked_at.is_(None)))
    if not token:
        raise HTTPException(status_code=401, detail="Invalid or revoked API token")
    user = db.get(User, token.user_id)
    if not user:
        raise HTTPException(status_code=401, detail="Token owner no longer exists")
    return token, user


@app.on_event("startup")
def startup() -> None:
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        if (db.scalar(select(func.count()).select_from(Game)) or 0) == 0:
            examples = [
                ("Hollow Knight", "Team Cherry", 2017, "A hand-drawn action-adventure through a ruined insect kingdom."),
                ("Stardew Valley", "ConcernedApe", 2016, "Build a life and farm in a welcoming valley."),
                ("Celeste", "Maddy Makes Games", 2018, "A precision platformer about climbing a mountain."),
                ("Minecraft", "Mojang Studios", 2011, "A sandbox game about building, exploring and crafting."),
                ("Outer Wilds", "Mobius Digital", 2019, "Explore a solar system caught in a time loop."),
                ("Portal 2", "Valve", 2011, "A puzzle adventure built around portal mechanics."),
            ]
            for title, developer, year, description in examples:
                db.add(Game(canonical_id=new_canonical_id(), title=title, developer=developer,
                            release_year=year, description=description))
            db.commit()


@app.get("/health")
def health(db: Db) -> dict:
    db.execute(select(1))
    return {"status": "ok", "database": "connected"}


@app.get("/api/v1/games")
def search_games(q: str = "", limit: int = 20, offset: int = 0, db: Db = Depends(get_db)) -> dict:
    limit, offset = max(1, min(limit, 100)), max(0, offset)
    statement = select(Game)
    if q.strip():
        pattern = f"%{q.strip()}%"
        statement = statement.where(Game.title.ilike(pattern) | Game.developer.ilike(pattern)
                                    | Game.canonical_id.ilike(pattern))
    total = db.scalar(select(func.count()).select_from(statement.subquery())) or 0
    games = db.scalars(statement.order_by(Game.title).offset(offset).limit(limit)).all()
    return {"items": [serialize_game(game) for game in games], "total": total, "limit": limit, "offset": offset}


@app.get("/api/v1/games/{canonical_id}")
def get_game(canonical_id: str, db: Db) -> dict:
    game = db.scalar(select(Game).where(Game.canonical_id == canonical_id))
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")
    return serialize_game(game)


@app.post("/api/v1/games", status_code=status.HTTP_201_CREATED)
def create_game(payload: GameCreate, db: Db, authorization: Annotated[str | None, Header()] = None) -> dict:
    _, user = require_api_token(db, authorization)
    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=422, detail="Title cannot be blank")
    game = Game(canonical_id=new_canonical_id(), title=title, developer=payload.developer.strip(),
                release_year=payload.release_year, description=payload.description.strip(), created_by=user.id)
    db.add(game)
    db.commit()
    db.refresh(game)
    return serialize_game(game)


@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Db):
    user = current_user(request, db)
    games = db.scalars(select(Game).order_by(Game.title).limit(100)).all()
    tokens = db.scalars(select(ApiToken).where(ApiToken.user_id == user.id).order_by(ApiToken.created_at.desc())).all() if user else []
    one_time_token = None
    flash_id = request.query_params.get("token_notice")
    if flash_id:
        flash = ONE_TIME_TOKENS.pop(flash_id, None)
        if flash and user and flash[0] == user.id and flash[2] > time.time():
            one_time_token = flash[1]
    expired = [key for key, value in ONE_TIME_TOKENS.items() if value[2] <= time.time()]
    for key in expired:
        ONE_TIME_TOKENS.pop(key, None)
    return templates.TemplateResponse(request, "index.html", {
        "app_name": APP_NAME, "user": user, "games": games, "tokens": tokens,
        "notice": request.query_params.get("notice"), "error": request.query_params.get("error"),
        "one_time_token": one_time_token,
    })


@app.post("/auth/register")
def register(request: Request, db: Db, email: Annotated[str, Form()],
             display_name: Annotated[str, Form()], password: Annotated[str, Form()]):
    email, display_name = email.strip().lower(), display_name.strip()
    if len(email) > 254 or "@" not in email or not display_name or len(display_name) > 80:
        return RedirectResponse("/?error=Please+enter+a+valid+email+and+display+name", status_code=303)
    if len(password) < 10 or len(password) > 256:
        return RedirectResponse("/?error=Password+must+be+10-256+characters", status_code=303)
    if db.scalar(select(User).where(User.email == email)):
        return RedirectResponse("/?error=An+account+with+that+email+already+exists", status_code=303)
    user = User(email=email, display_name=display_name, password_hash=hash_password(password))
    db.add(user)
    db.commit()
    db.refresh(user)
    request.session.clear()
    request.session["user_id"] = user.id
    return RedirectResponse("/?notice=Account+created", status_code=303)


@app.post("/auth/login")
def login(request: Request, db: Db, email: Annotated[str, Form()], password: Annotated[str, Form()]):
    user = db.scalar(select(User).where(User.email == email.strip().lower()))
    if not user or not verify_password(password, user.password_hash):
        return RedirectResponse("/?error=Email+or+password+was+not+recognised", status_code=303)
    request.session.clear()
    request.session["user_id"] = user.id
    return RedirectResponse("/?notice=Welcome+back", status_code=303)


@app.post("/auth/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)


@app.post("/tokens")
def create_token(request: Request, db: Db, label: Annotated[str, Form()]):
    user = current_user(request, db)
    if not user:
        return RedirectResponse("/?error=Sign+in+before+creating+a+key", status_code=303)
    label = label.strip()[:80]
    if not label:
        return RedirectResponse("/?error=Give+your+key+a+label", status_code=303)
    raw = "atlas_beta_" + secrets.token_urlsafe(32)
    db.add(ApiToken(user_id=user.id, label=label,
                    token_hash=hashlib.sha256(raw.encode("utf-8")).hexdigest(), token_prefix=raw[:16]))
    db.commit()
    flash_id = secrets.token_urlsafe(18)
    ONE_TIME_TOKENS[flash_id] = (user.id, raw, time.time() + 120)
    return RedirectResponse("/?notice=API+key+created&token_notice=" + flash_id, status_code=303)


@app.post("/tokens/{token_id}/revoke")
def revoke_token(token_id: int, request: Request, db: Db):
    user = current_user(request, db)
    if not user:
        return RedirectResponse("/?error=Sign+in+required", status_code=303)
    token = db.scalar(select(ApiToken).where(ApiToken.id == token_id, ApiToken.user_id == user.id))
    if not token:
        return RedirectResponse("/?error=Key+not+found", status_code=303)
    token.revoked_at = utcnow()
    db.commit()
    return RedirectResponse("/?notice=API+key+revoked", status_code=303)


@app.post("/games")
def add_game_form(request: Request, db: Db, title: Annotated[str, Form()],
                  developer: Annotated[str, Form()] = "", release_year: Annotated[str, Form()] = "",
                  description: Annotated[str, Form()] = ""):
    user = current_user(request, db)
    if not user:
        return RedirectResponse("/?error=Sign+in+to+add+catalogue+records", status_code=303)
    title = title.strip()
    if not title or len(title) > 200:
        return RedirectResponse("/?error=Title+is+required+and+must+be+under+200+characters", status_code=303)
    try:
        year = int(release_year) if release_year.strip() else None
        if year is not None and not 1950 <= year <= 2100:
            raise ValueError
    except ValueError:
        return RedirectResponse("/?error=Release+year+must+be+between+1950+and+2100", status_code=303)
    db.add(Game(canonical_id=new_canonical_id(), title=title, developer=developer.strip()[:200],
                release_year=year, description=description.strip()[:3000], created_by=user.id))
    db.commit()
    return RedirectResponse("/?notice=Game+added+to+the+mock+catalogue", status_code=303)
