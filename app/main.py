from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
import uuid
from pathlib import Path
from datetime import datetime, timezone
from typing import Annotated

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine, func, inspect, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from starlette.middleware.sessions import SessionMiddleware

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:////data/atlas.db")
SESSION_SECRET = os.getenv("SESSION_SECRET", "dev-only-change-this-session-secret")
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() == "true"
APP_NAME = os.getenv("APP_NAME", "Atlas Metadata")
ADMIN_EMAILS = {value.strip().lower() for value in os.getenv("ADMIN_EMAILS", "").split(",") if value.strip()}
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", os.path.join(BASE_DIR, "uploads")))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
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
    custom_fields: Mapped[str] = mapped_column(Text, default="{}")
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class GameCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    developer: str = Field(default="", max_length=200)
    release_year: int | None = Field(default=None, ge=1950, le=2100)
    description: str = Field(default="", max_length=3000)
    custom_fields: dict[str, object] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list, max_length=40)
    external_ids: list[dict[str, str]] = Field(default_factory=list, max_length=30)


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


def serialize_game(game: Game, db: Session | None = None) -> dict:
    result = {"id": game.canonical_id, "title": game.title, "developer": game.developer,
              "release_year": game.release_year, "description": game.description,
              "custom_fields": json.loads(game.custom_fields or "{}"),
              "created_at": game.created_at.isoformat() if game.created_at else None}
    if db is not None:
        result["tags"] = [tag.name for tag in db.scalars(
            select(Tag).join(GameTag, GameTag.tag_id == Tag.id).where(GameTag.game_id == game.id).order_by(Tag.name)
        ).all()]
        result["external_ids"] = [
            {"provider": provider.slug, "provider_name": provider.name, "external_id": ext.external_id}
            for ext, provider in db.execute(
                select(GameExternalId, MetadataProvider)
                .join(MetadataProvider, MetadataProvider.id == GameExternalId.provider_id)
                .where(GameExternalId.game_id == game.id).order_by(MetadataProvider.slug)
            ).all()
        ]
        result["images"] = [
            {"url": image.url, "content_type": image.content_type, "created_at": image.created_at.isoformat()}
            for image in db.scalars(select(GameImage).where(GameImage.game_id == game.id).order_by(GameImage.created_at)).all()
        ]
    return result


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
    # create_all does not alter an existing database volume when a model gains a column.
    columns = {column["name"] for column in inspect(engine).get_columns("games")}
    if "custom_fields" not in columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE games ADD COLUMN custom_fields TEXT NOT NULL DEFAULT '{}'"))
    with SessionLocal() as db:
        if not db.scalar(select(SchemaDefinition).where(SchemaDefinition.name == "game")):
            db.add(SchemaDefinition(name="game", display_name="Game", schema_json="[]"))
            db.commit()
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
def search_games(db: Db, q: str = "", limit: int = 20, offset: int = 0, authorization: Annotated[str | None, Header()] = None) -> dict:
    require_api_token(db, authorization)
    limit, offset = max(1, min(limit, 100)), max(0, offset)
    statement = select(Game)
    if q.strip():
        pattern = f"%{q.strip()}%"
        statement = statement.where(Game.title.ilike(pattern) | Game.developer.ilike(pattern)
                                    | Game.canonical_id.ilike(pattern))
    total = db.scalar(select(func.count()).select_from(statement.subquery())) or 0
    games = db.scalars(statement.order_by(Game.title).offset(offset).limit(limit)).all()
    return {"items": [serialize_game(game, db) for game in games], "total": total, "limit": limit, "offset": offset}


@app.get("/api/v1/games/{canonical_id}")
def get_game(canonical_id: str, db: Db, authorization: Annotated[str | None, Header()] = None) -> dict:
    require_api_token(db, authorization)
    game = db.scalar(select(Game).where(Game.canonical_id == canonical_id))
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")
    return serialize_game(game, db)


@app.post("/api/v1/games", status_code=status.HTTP_201_CREATED)
def create_game(payload: GameCreate, db: Db, authorization: Annotated[str | None, Header()] = None) -> dict:
    _, user = require_api_token(db, authorization)
    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=422, detail="Title cannot be blank")
    custom_fields = validate_custom_fields(db, payload.custom_fields)
    game = Game(canonical_id=new_canonical_id(), title=title, developer=payload.developer.strip(),
                release_year=payload.release_year, description=payload.description.strip(),
                custom_fields=json.dumps(custom_fields), created_by=user.id)
    db.add(game)
    db.flush()
    apply_tags(db, game, payload.tags)
    apply_external_ids(db, game, payload.external_ids)
    db.commit()
    db.refresh(game)
    return serialize_game(game, db)


@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Db):
    user = current_user(request, db)
    games = db.scalars(select(Game).order_by(Game.title).limit(100)).all() if user else []
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
        "one_time_token": one_time_token, "is_admin": bool(user and user.email.lower() in ADMIN_EMAILS),
        "schema_fields": json.loads((db.scalar(select(SchemaDefinition).where(SchemaDefinition.name == "game")).schema_json) if db.scalar(select(SchemaDefinition).where(SchemaDefinition.name == "game")) else "[]"),
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
async def add_game_form(request: Request, db: Db):
    user = current_user(request, db)
    if not user:
        return RedirectResponse("/?error=Sign+in+to+add+catalogue+records", status_code=303)
    form = await request.form()
    title = str(form.get("title", "")).strip()
    developer = str(form.get("developer", "")).strip()
    release_year = str(form.get("release_year", "")).strip()
    description = str(form.get("description", "")).strip()
    if not title or len(title) > 200:
        return RedirectResponse("/?error=Title+is+required+and+must+be+under+200+characters", status_code=303)
    try:
        year = int(release_year) if release_year else None
        if year is not None and not 1950 <= year <= 2100:
            raise ValueError
    except ValueError:
        return RedirectResponse("/?error=Release+year+must+be+between+1950+and+2100", status_code=303)
    schema = db.scalar(select(SchemaDefinition).where(SchemaDefinition.name == "game"))
    definitions = validate_schema_document(schema.schema_json) if schema else []
    values = {}
    try:
        for definition in definitions:
            key = "custom__" + definition["key"]
            raw = form.getlist(key) if definition.get("multiple") and definition.get("type") == "enum" else form.get(key)
            if definition.get("multiple") and definition.get("type") != "enum" and isinstance(raw, str):
                raw = [part.strip() for part in raw.split(",") if part.strip()]
            if raw is None or raw == "":
                continue
            if definition.get("type") == "integer":
                raw = [int(v) for v in raw] if isinstance(raw, list) else int(raw)
            elif definition.get("type") == "number":
                raw = [float(v) for v in raw] if isinstance(raw, list) else float(raw)
            elif definition.get("type") == "boolean":
                raw = str(raw).lower() == "true"
            values[definition["key"]] = raw
        custom_fields = validate_custom_fields(db, values)
        tags = [item.strip() for item in str(form.get("tags", "")).split(",") if item.strip()]
        external_ids_raw = str(form.get("external_ids_json", "[]")).strip() or "[]"
        external_ids = json.loads(external_ids_raw)
        if not isinstance(external_ids, list) or any(not isinstance(item, dict) for item in external_ids):
            raise ValueError("External IDs must be a JSON array of objects")
    except (ValueError, json.JSONDecodeError, HTTPException) as exc:
        detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
        from urllib.parse import quote_plus
        return RedirectResponse("/?error=" + quote_plus(str(detail)), status_code=303)
    game = Game(canonical_id=new_canonical_id(), title=title, developer=developer[:200],
                release_year=year, description=description[:3000], custom_fields=json.dumps(custom_fields), created_by=user.id)
    try:
        db.add(game)
        db.flush()
        apply_tags(db, game, tags)
        apply_external_ids(db, game, external_ids)
        db.commit()
    except HTTPException as exc:
        db.rollback()
        from urllib.parse import quote_plus
        return RedirectResponse("/?error=" + quote_plus(str(exc.detail)), status_code=303)
    return RedirectResponse("/?notice=Game+added+to+the+catalogue", status_code=303)


# Extensible catalogue structures. These are deliberately relational where identity matters,
# while custom game attributes are validated against the administrator-managed schema.
class SchemaDefinition(Base):
    __tablename__ = "schema_definitions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(120))
    schema_json: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class MetadataProvider(Base):
    __tablename__ = "metadata_providers"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    homepage: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GameExternalId(Base):
    __tablename__ = "game_external_ids"
    __table_args__ = (UniqueConstraint("provider_id", "external_id", name="uq_provider_external_id"),
                      UniqueConstraint("game_id", "provider_id", name="uq_game_provider"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"), index=True)
    provider_id: Mapped[int] = mapped_column(ForeignKey("metadata_providers.id", ondelete="CASCADE"), index=True)
    external_id: Mapped[str] = mapped_column(String(200), index=True)


class Tag(Base):
    __tablename__ = "tags"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True, index=True)


class GameTag(Base):
    __tablename__ = "game_tags"
    __table_args__ = (UniqueConstraint("game_id", "tag_id", name="uq_game_tag"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"), index=True)
    tag_id: Mapped[int] = mapped_column(ForeignKey("tags.id", ondelete="CASCADE"), index=True)


class GameImage(Base):
    __tablename__ = "game_images"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"), index=True)
    url: Mapped[str] = mapped_column(String(500))
    content_type: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


def require_admin(request: Request, db: Session) -> User:
    user = current_user(request, db)
    if not user:
        raise HTTPException(status_code=303, detail="Sign in required", headers={"Location": "/#account"})
    if user.email.lower() not in ADMIN_EMAILS:
        raise HTTPException(status_code=403, detail="Administrator access is not enabled for this account")
    return user


def validate_schema_document(raw: str) -> list[dict]:
    try:
        fields = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail=f"Schema JSON is invalid: {exc.msg}") from exc
    if not isinstance(fields, list) or len(fields) > 100:
        raise HTTPException(status_code=422, detail="A schema must be a JSON array of at most 100 field definitions")
    seen = set()
    allowed_types = {"string", "integer", "number", "boolean", "date", "url", "enum"}
    for item in fields:
        if not isinstance(item, dict):
            raise HTTPException(status_code=422, detail="Each schema field must be an object")
        key, label, kind = item.get("key"), item.get("label"), item.get("type", "string")
        if not isinstance(key, str) or not key or len(key) > 64 or not key.replace("_", "").isalnum() or key[0].isdigit():
            raise HTTPException(status_code=422, detail="Field keys must be short identifiers using letters, digits and underscores")
        if key in seen or key in {"id", "title", "canonical_id", "created_at"}:
            raise HTTPException(status_code=422, detail=f"Duplicate or reserved field key: {key}")
        if not isinstance(label, str) or not label.strip() or len(label) > 120 or not isinstance(kind, str) or kind not in allowed_types:
            raise HTTPException(status_code=422, detail=f"Field {key} needs a label and a supported type")
        if not isinstance(item.get("required", False), bool) or not isinstance(item.get("multiple", False), bool):
            raise HTTPException(status_code=422, detail=f"Field {key}: required and multiple must be booleans")
        if kind == "boolean" and item.get("multiple", False):
            raise HTTPException(status_code=422, detail=f"Boolean field {key} cannot be multi-value")
        if kind == "enum":
            options = item.get("options")
            if not isinstance(options, list) or not options or len(options) > 100 or any(not isinstance(option, str) or not option or len(option) > 120 for option in options):
                raise HTTPException(status_code=422, detail=f"Enum field {key} needs a non-empty array of short string options")
        seen.add(key)
    return fields


def validate_custom_fields(db: Session, values: dict[str, object]) -> dict[str, object]:
    schema = db.scalar(select(SchemaDefinition).where(SchemaDefinition.name == "game"))
    if not schema:
        if values:
            raise HTTPException(status_code=422, detail="No game custom-field schema is configured yet")
        return {}
    fields = validate_schema_document(schema.schema_json)
    definitions = {field["key"]: field for field in fields}
    unknown = set(values) - set(definitions)
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unknown custom fields: {', '.join(sorted(unknown))}")
    cleaned = {}
    for key, definition in definitions.items():
        value = values.get(key)
        if value is None or value == "":
            if definition.get("required"):
                raise HTTPException(status_code=422, detail=f"Missing required custom field: {key}")
            continue
        if definition.get("multiple") and isinstance(value, list) and not value:
            if definition.get("required"):
                raise HTTPException(status_code=422, detail=f"Missing required custom field: {key}")
            continue
        values_to_check = value if definition.get("multiple") else [value]
        if definition.get("multiple") and not isinstance(value, list):
            raise HTTPException(status_code=422, detail=f"Field {key} must be an array")
        if not definition.get("multiple") and isinstance(value, list):
            raise HTTPException(status_code=422, detail=f"Field {key} must contain a single value")
        for item in values_to_check:
            kind = definition.get("type", "string")
            valid = (
                (kind in {"string", "date", "url"} and isinstance(item, str)) or
                (kind == "integer" and isinstance(item, int) and not isinstance(item, bool)) or
                (kind == "number" and isinstance(item, (int, float)) and not isinstance(item, bool)) or
                (kind == "boolean" and isinstance(item, bool)) or
                (kind == "enum" and item in definition.get("options", []))
            )
            if not valid:
                raise HTTPException(status_code=422, detail=f"Invalid value for custom field: {key}")
            if isinstance(item, str) and len(item) > 4000:
                raise HTTPException(status_code=422, detail=f"Value for {key} is too long")
        cleaned[key] = values_to_check if definition.get("multiple") else values
    return cleaned


def apply_tags(db: Session, game: Game, tag_names: list[str]) -> None:
    if len(tag_names) > 40:
        raise HTTPException(status_code=422, detail="At most 40 tags may be attached to a game")
    normalized = []
    for raw in tag_names:
        name = raw.strip().lower()
        if not name or len(name) > 80:
            raise HTTPException(status_code=422, detail="Tags must contain 1-80 characters")
        if name not in normalized:
            normalized.append(name)
    for name in normalized:
        tag = db.scalar(select(Tag).where(Tag.name == name))
        if not tag:
            tag = Tag(name=name)
            db.add(tag)
            db.flush()
        db.add(GameTag(game_id=game.id, tag_id=tag.id))


def apply_external_ids(db: Session, game: Game, external_ids: list[dict[str, str]]) -> None:
    if len(external_ids) > 30:
        raise HTTPException(status_code=422, detail="At most 30 external IDs may be attached to a game")
    providers_seen = set()
    for item in external_ids:
        raw_slug, raw_external_id = item.get("provider", ""), item.get("external_id", "")
        if not isinstance(raw_slug, str) or not isinstance(raw_external_id, str):
            raise HTTPException(status_code=422, detail="Provider slug and external ID must be strings")
        slug, external_id = raw_slug.strip().lower(), raw_external_id.strip()
        if not slug or len(slug) > 80 or not external_id or len(external_id) > 200:
            raise HTTPException(status_code=422, detail="Each external ID needs a provider slug and an ID (maximum 200 characters)")
        if slug in providers_seen:
            raise HTTPException(status_code=422, detail=f"Only one ID per provider is allowed per game: {slug}")
        providers_seen.add(slug)
        provider = db.scalar(select(MetadataProvider).where(MetadataProvider.slug == slug))
        if not provider:
            raise HTTPException(status_code=422, detail=f"Unknown metadata provider: {slug}. An administrator must register it first.")
        if db.scalar(select(GameExternalId).where(GameExternalId.provider_id == provider.id, GameExternalId.external_id == external_id)):
            raise HTTPException(status_code=409, detail=f"External ID {external_id} is already mapped for provider {slug}")
        db.add(GameExternalId(game_id=game.id, provider_id=provider.id, external_id=external_id))


@app.get("/api/v1/schemas")
def api_schemas(db: Db, authorization: Annotated[str | None, Header()] = None):
    require_api_token(db, authorization)
    schemas = db.scalars(select(SchemaDefinition).order_by(SchemaDefinition.name)).all()
    return {"items": [{"name": item.name, "display_name": item.display_name, "fields": json.loads(item.schema_json),
                       "updated_at": item.updated_at.isoformat() if item.updated_at else None} for item in schemas]}


@app.get("/api/v1/providers")
def api_providers(db: Db, authorization: Annotated[str | None, Header()] = None):
    require_api_token(db, authorization)
    providers = db.scalars(select(MetadataProvider).order_by(MetadataProvider.slug)).all()
    return {"items": [{"slug": provider.slug, "name": provider.name, "homepage": provider.homepage or None}
                      for provider in providers]}


@app.get("/catalogue/search")
def browser_search(request: Request, db: Db, q: str = "", limit: int = 100, offset: int = 0):
    if not current_user(request, db):
        raise HTTPException(status_code=401, detail="Sign in to search the catalogue")
    limit, offset = max(1, min(limit, 100)), max(0, offset)
    statement = select(Game)
    if q.strip():
        pattern = f"%{q.strip()}%"
        statement = statement.where(Game.title.ilike(pattern) | Game.developer.ilike(pattern) |
                                    Game.canonical_id.ilike(pattern))
    total = db.scalar(select(func.count()).select_from(statement.subquery())) or 0
    games = db.scalars(statement.order_by(Game.title).offset(offset).limit(limit)).all()
    return {"items": [serialize_game(game, db) for game in games], "total": total, "limit": limit, "offset": offset}


@app.get("/admin", response_class=HTMLResponse)
def admin_home(request: Request, db: Db):
    user = current_user(request, db)
    if not user:
        return RedirectResponse("/#account", status_code=303)
    if user.email.lower() not in ADMIN_EMAILS:
        raise HTTPException(status_code=403, detail="Administrator access is not enabled for this account")
    schemas = db.scalars(select(SchemaDefinition).order_by(SchemaDefinition.name)).all()
    providers = db.scalars(select(MetadataProvider).order_by(MetadataProvider.slug)).all()
    schema_json = next((item.schema_json for item in schemas if item.name == "game"), "[]")
    return templates.TemplateResponse(request, "admin.html", {
        "app_name": APP_NAME, "user": user, "schemas": schemas, "providers": providers,
        "schema_json": schema_json, "notice": request.query_params.get("notice"),
        "error": request.query_params.get("error"),
    })


@app.post("/admin/schemas/save")
def save_schema(request: Request, db: Db, name: Annotated[str, Form()],
                display_name: Annotated[str, Form()], schema_json: Annotated[str, Form()]):
    require_admin(request, db)
    name = name.strip().lower()
    display_name = display_name.strip()
    if not name or len(name) > 80 or not name.replace("_", "").isalnum() or not display_name or len(display_name) > 120:
        return RedirectResponse("/admin?error=Schema+name+or+display+name+is+invalid", status_code=303)
    try:
        fields = validate_schema_document(schema_json)
    except HTTPException as exc:
        return RedirectResponse("/admin?error=" + __import__("urllib.parse", fromlist=["quote_plus"]).quote_plus(str(exc.detail)), status_code=303)
    schema = db.scalar(select(SchemaDefinition).where(SchemaDefinition.name == name))
    if schema:
        schema.display_name, schema.schema_json, schema.updated_at = display_name, json.dumps(fields, indent=2), utcnow()
    else:
        db.add(SchemaDefinition(name=name, display_name=display_name, schema_json=json.dumps(fields, indent=2)))
    db.commit()
    return RedirectResponse("/admin?notice=Schema+saved", status_code=303)


@app.get("/admin/schemas/{schema_id}/export.json")
def export_schema(schema_id: int, request: Request, db: Db):
    require_admin(request, db)
    schema = db.get(SchemaDefinition, schema_id)
    if not schema:
        raise HTTPException(status_code=404, detail="Schema not found")
    from fastapi.responses import JSONResponse
    return JSONResponse({"name": schema.name, "display_name": schema.display_name, "fields": json.loads(schema.schema_json)},
                        headers={"Content-Disposition": f'attachment; filename="{schema.name}-schema.json"'})


@app.post("/admin/providers")
def add_provider(request: Request, db: Db, slug: Annotated[str, Form()],
                 name: Annotated[str, Form()], homepage: Annotated[str, Form()] = ""):
    require_admin(request, db)
    slug = slug.strip().lower()
    name, homepage = name.strip(), homepage.strip()
    if not slug or len(slug) > 80 or not slug.replace("-", "").replace("_", "").isalnum() or not name or len(name) > 120:
        return RedirectResponse("/admin?error=Provider+slug+or+name+is+invalid", status_code=303)
    if homepage and (not homepage.startswith("https://") or len(homepage) > 500):
        return RedirectResponse("/admin?error=Provider+homepage+must+be+an+HTTPS+URL", status_code=303)
    existing = db.scalar(select(MetadataProvider).where(MetadataProvider.slug == slug))
    if existing:
        existing.name, existing.homepage = name, homepage
    else:
        db.add(MetadataProvider(slug=slug, name=name, homepage=homepage))
    db.commit()
    return RedirectResponse("/admin?notice=Provider+saved", status_code=303)


@app.get("/api/v1/images/{filename}")
def get_uploaded_image(filename: str, db: Db, authorization: Annotated[str | None, Header()] = None):
    require_api_token(db, authorization)
    if Path(filename).name != filename or not filename.replace(".", "").isalnum():
        raise HTTPException(status_code=404, detail="Image not found")
    image_row = db.scalar(select(GameImage).where(GameImage.url == "/api/v1/images/" + filename))
    target = UPLOAD_DIR / filename
    if not image_row or not target.is_file():
        raise HTTPException(status_code=404, detail="Image not found")
    return FileResponse(target, media_type=image_row.content_type, filename=filename, content_disposition_type="inline",
                        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=300"})


@app.post("/api/v1/games/{canonical_id}/images", status_code=status.HTTP_201_CREATED)
async def upload_game_image(canonical_id: str, db: Db, file: UploadFile = File(...),
                            authorization: Annotated[str | None, Header()] = None):
    try:
        require_api_token(db, authorization)
        game = db.scalar(select(Game).where(Game.canonical_id == canonical_id))
        if not game:
            raise HTTPException(status_code=404, detail="Game not found")
        allowed = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}
        if file.content_type not in allowed:
            raise HTTPException(status_code=415, detail="Only PNG, JPEG, WebP and GIF images are accepted")
        content = await file.read(5 * 1024 * 1024 + 1)
        if not content or len(content) > 5 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="Images must be between 1 byte and 5 MiB")
        signatures = {"image/png": b"\x89PNG\r\n\x1a\n", "image/jpeg": b"\xff\xd8\xff",
                      "image/gif": (b"GIF87a", b"GIF89a"), "image/webp": b"RIFF"}
        signature = signatures[file.content_type]
        if not (any(content.startswith(sig) for sig in signature) if isinstance(signature, tuple) else content.startswith(signature)):
            raise HTTPException(status_code=415, detail="File contents do not match the declared image type")
        if file.content_type == "image/webp" and content[8:12] != b"WEBP":
            raise HTTPException(status_code=415, detail="Invalid WebP image")
        filename = uuid.uuid4().hex + allowed[file.content_type]
        target = UPLOAD_DIR / filename
        target.write_bytes(content)
        image_row = GameImage(game_id=game.id, url="/api/v1/images/" + filename, content_type=file.content_type)
        db.add(image_row)
        db.commit()
        return {"id": image_row.id, "game_id": canonical_id, "url": image_row.url, "content_type": image_row.content_type}
    finally:
        await file.close()
