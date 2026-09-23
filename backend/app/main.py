import json
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings
from sqlalchemy import DateTime, Float, Integer, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from app.rules import classify


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


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    value: Mapped[str] = mapped_column(String(120))


class HeatSnapshot(Base):
    __tablename__ = "heat_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    prefix_len: Mapped[int] = mapped_column(Integer)
    # 冻结内容：[{"prefix": "回风", "alarm": 1, "ok": 0, "row_ids": [2]}, ...]
    groups_json: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LoginIn(BaseModel):
    username: str
    password: str


class ReadingIn(BaseModel):
    site: str = Field(min_length=1, max_length=80)
    ch4_pct: float


class PrefixLenIn(BaseModel):
    prefix_len: int = Field(ge=1, le=20)


DEFAULT_PREFIX_LEN = 2


def get_prefix_len(db: Session) -> int:
    row = db.get(Setting, "prefix_len")
    if row is None:
        return DEFAULT_PREFIX_LEN
    try:
        value = int(row.value)
    except ValueError:
        return DEFAULT_PREFIX_LEN
    return value if 1 <= value <= 20 else DEFAULT_PREFIX_LEN


def set_prefix_len(db: Session, prefix_len: int) -> None:
    row = db.get(Setting, "prefix_len")
    if row is None:
        db.add(Setting(key="prefix_len", value=str(prefix_len)))
    else:
        row.value = str(prefix_len)
    db.commit()


def heat_groups(db: Session, prefix_len: int) -> list[dict]:
    """按巷道名前 prefix_len 个字分组，统计报警/正常条数与命中行编号。"""
    groups: dict[str, dict] = {}
    for r in db.query(Reading).order_by(Reading.id.asc()).all():
        prefix = r.site.strip()[:prefix_len]
        bucket = groups.setdefault(
            prefix,
            {"prefix": prefix, "alarm": 0, "ok": 0, "row_ids": []},
        )
        if r.level == "报警":
            bucket["alarm"] += 1
        else:
            bucket["ok"] += 1
        bucket["row_ids"].append(r.id)
    return sorted(groups.values(), key=lambda g: (-g["alarm"], g["prefix"]))


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


@app.get("/api/heat/prefix-len")
def get_heat_prefix_len(_user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        return {"prefix_len": get_prefix_len(db)}
    finally:
        db.close()


@app.put("/api/heat/prefix-len")
def update_heat_prefix_len(body: PrefixLenIn, user: dict = Depends(require_writer)):
    db = SessionLocal()
    try:
        set_prefix_len(db, body.prefix_len)
    finally:
        db.close()
    return {"prefix_len": body.prefix_len}


@app.get("/api/heat/table")
def heat_table(_user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        prefix_len = get_prefix_len(db)
        return {"prefix_len": prefix_len, "groups": heat_groups(db, prefix_len)}
    finally:
        db.close()


@app.get("/api/heat/snapshots")
def list_heat_snapshots(_user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        rows = db.query(HeatSnapshot).order_by(HeatSnapshot.id.desc()).all()
        return [
            {
                "id": s.id,
                "prefix_len": s.prefix_len,
                "created_by": s.created_by,
                "created_at": s.created_at.isoformat(),
                "group_count": len(json.loads(s.groups_json)),
            }
            for s in rows
        ]
    finally:
        db.close()


@app.post("/api/heat/snapshots", status_code=201)
def create_heat_snapshot(user: dict = Depends(require_writer)):
    db = SessionLocal()
    try:
        prefix_len = get_prefix_len(db)
        groups = heat_groups(db, prefix_len)
        row = HeatSnapshot(
            prefix_len=prefix_len,
            groups_json=json.dumps(groups, ensure_ascii=False),
            created_by=user["username"],
            created_at=datetime.now(timezone.utc),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return {
            "id": row.id,
            "prefix_len": row.prefix_len,
            "groups": groups,
            "created_by": row.created_by,
            "created_at": row.created_at.isoformat(),
        }
    finally:
        db.close()


@app.get("/api/heat/snapshots/{snapshot_id}")
def get_heat_snapshot(snapshot_id: int, _user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        row = db.get(HeatSnapshot, snapshot_id)
        if row is None:
            raise HTTPException(status_code=404, detail="快照不存在")
        return {
            "id": row.id,
            "prefix_len": row.prefix_len,
            "groups": json.loads(row.groups_json),
            "created_by": row.created_by,
            "created_at": row.created_at.isoformat(),
        }
    finally:
        db.close()


@app.websocket("/ws/alerts")
async def alerts(ws: WebSocket):
    await ws.accept()
    sockets.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        sockets.discard(ws)