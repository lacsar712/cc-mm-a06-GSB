import json
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings
from sqlalchemy import DateTime, Float, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from app.rules import classify

PREFIX_MIN = 1
PREFIX_MAX = 8
DEFAULT_PREFIX_LENGTH = 2


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg2://app:app@localhost:54391/methane"
    jwt_secret: str = "mine-methane-dev-secret"


settings = Settings()
pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer(auto_error=False)
USERS = {
    "gasman": {"role": "writer", "password_hash": pwd.hash("gas123456")},
    "viewer": {"role": "reader", "password_hash": pwd.hash("view123456")},
}

engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine)


class Base(DeclarativeBase):
    pass


class Reading(Base):
    __tablename__ = "readings"
    id: Mapped[int] = mapped_column(primary_key=True)
    site: Mapped[str] = mapped_column(String(80))
    ch4_pct: Mapped[float] = mapped_column(Float)
    level: Mapped[str] = mapped_column(String(20))
    note: Mapped[str] = mapped_column(String(200))
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class HeatSetting(Base):
    __tablename__ = "heat_settings"
    id: Mapped[int] = mapped_column(primary_key=True)
    prefix_length: Mapped[int]


class HeatSnapshot(Base):
    __tablename__ = "heat_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    prefix_length: Mapped[int]
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    groups_json: Mapped[str] = mapped_column(Text)
    hit_ids_json: Mapped[str] = mapped_column(Text)


class LoginIn(BaseModel):
    username: str
    password: str


class ReadingIn(BaseModel):
    site: str = Field(min_length=1, max_length=80)
    ch4_pct: float


class PrefixLengthIn(BaseModel):
    prefix_length: int = Field(ge=PREFIX_MIN, le=PREFIX_MAX)


def current_user(credentials: HTTPAuthorizationCredentials | None = Depends(security)) -> dict:
    if credentials is None:
        raise HTTPException(status_code=401, detail="未登录")
    try:
        payload = jwt.decode(credentials.credentials, settings.jwt_secret, algorithms=["HS256"])
    except JWTError as exc:
        raise HTTPException(status_code=401, detail="无效令牌") from exc
    username = payload.get("sub")
    if username not in USERS:
        raise HTTPException(status_code=401, detail="无效令牌")
    return {"username": username, "role": payload.get("role")}


def require_writer(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "writer":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="仅瓦斯检查员可上报")
    return user


def require_inspector(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "writer":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="仅瓦斯检查员可操作")
    return user


sockets: set[WebSocket] = set()
app = FastAPI(title="矿井瓦斯班测台")


@app.on_event("startup")
def startup():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        if db.query(Reading).count() == 0:
            now = datetime.now(timezone.utc)
            for site, ch4 in (("东翼-12", 0.35), ("回风巷", 1.4)):
                level, note = classify(ch4)
                db.add(
                    Reading(
                        site=site,
                        ch4_pct=ch4,
                        level=level,
                        note=note,
                        created_by="gasman",
                        created_at=now,
                    )
                )
        if db.query(HeatSetting).count() == 0:
            db.add(HeatSetting(id=1, prefix_length=DEFAULT_PREFIX_LENGTH))
        db.commit()
    finally:
        db.close()


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "mine-methane-shift"}


@app.post("/api/auth/login")
def login(body: LoginIn):
    user = USERS.get(body.username.strip())
    if not user or not pwd.verify(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    exp = datetime.now(timezone.utc) + timedelta(hours=8)
    token = jwt.encode(
        {"sub": body.username.strip(), "role": user["role"], "exp": exp},
        settings.jwt_secret,
        algorithm="HS256",
    )
    return {"access_token": token, "username": body.username.strip(), "role": user["role"]}


@app.get("/api/readings")
def list_readings(_user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        rows = db.query(Reading).order_by(Reading.id.desc()).all()
        return [
            {
                "id": r.id,
                "site": r.site,
                "ch4_pct": r.ch4_pct,
                "level": r.level,
                "note": r.note,
                "created_by": r.created_by,
            }
            for r in rows
        ]
    finally:
        db.close()


@app.post("/api/readings", status_code=201)
async def create_reading(body: ReadingIn, user: dict = Depends(require_writer)):
    level, note = classify(body.ch4_pct)
    db = SessionLocal()
    try:
        row = Reading(
            site=body.site.strip(),
            ch4_pct=body.ch4_pct,
            level=level,
            note=note,
            created_by=user["username"],
            created_at=datetime.now(timezone.utc),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        payload = {"id": row.id, "site": row.site, "ch4_pct": row.ch4_pct, "level": row.level, "note": row.note}
    finally:
        db.close()
    dead = []
    for ws in list(sockets):
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        sockets.discard(ws)
    return payload


@app.websocket("/ws/alerts")
async def alerts(ws: WebSocket):
    await ws.accept()
    sockets.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        sockets.discard(ws)


def build_heat_groups(db: Session, prefix_length: int) -> list[dict]:
    rows = db.query(Reading).order_by(Reading.id.asc()).all()
    groups: dict[str, dict] = {}
    for r in rows:
        key = r.site[:prefix_length]
        bucket = groups.get(key)
        if bucket is None:
            bucket = {"prefix": key, "alarm_count": 0, "normal_count": 0, "hit_ids": []}
            groups[key] = bucket
        if r.level == "报警":
            bucket["alarm_count"] += 1
            bucket["hit_ids"].append(r.id)
        else:
            bucket["normal_count"] += 1
    return list(groups.values())


def get_prefix_length(db: Session) -> int:
    setting = db.get(HeatSetting, 1)
    return setting.prefix_length if setting else DEFAULT_PREFIX_LENGTH


def serialize_snapshot(snap: HeatSnapshot) -> dict:
    return {
        "id": snap.id,
        "prefix_length": snap.prefix_length,
        "created_by": snap.created_by,
        "created_at": snap.created_at.isoformat(),
        "groups": json.loads(snap.groups_json),
        "hit_ids": json.loads(snap.hit_ids_json),
    }


@app.get("/api/heat/live")
def heat_live(_user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        prefix_length = get_prefix_length(db)
        return {"prefix_length": prefix_length, "groups": build_heat_groups(db, prefix_length)}
    finally:
        db.close()


@app.put("/api/heat/prefix")
def set_heat_prefix(body: PrefixLengthIn, _user: dict = Depends(require_inspector)):
    db = SessionLocal()
    try:
        setting = db.get(HeatSetting, 1)
        if setting is None:
            setting = HeatSetting(id=1, prefix_length=body.prefix_length)
            db.add(setting)
        else:
            setting.prefix_length = body.prefix_length
        db.commit()
        return {"prefix_length": setting.prefix_length}
    finally:
        db.close()


@app.post("/api/heat/snapshots", status_code=201)
def create_heat_snapshot(user: dict = Depends(require_inspector)):
    db = SessionLocal()
    try:
        prefix_length = get_prefix_length(db)
        groups = build_heat_groups(db, prefix_length)
        hit_ids = sorted(hid for g in groups for hid in g["hit_ids"])
        snap = HeatSnapshot(
            prefix_length=prefix_length,
            created_by=user["username"],
            created_at=datetime.now(timezone.utc),
            groups_json=json.dumps(groups, ensure_ascii=False),
            hit_ids_json=json.dumps(hit_ids),
        )
        db.add(snap)
        db.commit()
        db.refresh(snap)
        return serialize_snapshot(snap)
    finally:
        db.close()


@app.get("/api/heat/snapshots")
def list_heat_snapshots(_user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        snaps = db.query(HeatSnapshot).order_by(HeatSnapshot.id.desc()).all()
        return [
            {
                "id": s.id,
                "prefix_length": s.prefix_length,
                "created_by": s.created_by,
                "created_at": s.created_at.isoformat(),
                "hit_ids": json.loads(s.hit_ids_json),
            }
            for s in snaps
        ]
    finally:
        db.close()


@app.get("/api/heat/snapshots/{snapshot_id}")
def get_heat_snapshot(snapshot_id: int, _user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        snap = db.get(HeatSnapshot, snapshot_id)
        if snap is None:
            raise HTTPException(status_code=404, detail="快照不存在")
        return serialize_snapshot(snap)
    finally:
        db.close()
