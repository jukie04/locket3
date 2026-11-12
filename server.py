import json
import os
import urllib.error
import urllib.request
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor

import locketlam
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from locketlam import LocketClient
from locketlam.exceptions import AuthenticationError

app = FastAPI()


def get_client(email: str, password: str) -> LocketClient:
    # Vercel chỉ cho ghi vào /tmp
    client = LocketClient(session_path="/tmp/locketlam_session.json")
    client.login(email, password)
    return client


def handle(e: Exception):
    if isinstance(e, AuthenticationError):
        raise HTTPException(status_code=401, detail="Sai email hoặc mật khẩu")
    raise HTTPException(status_code=400, detail=str(e))


def friend_uids(client: LocketClient) -> list:
    """Chỉ lấy UID bạn bè (có phân trang), nhanh hơn lấy đủ hồ sơ."""
    token = client.session.get_valid_token()
    base = (
        "https://firestore.googleapis.com/v1/projects/locket-4252a/databases/(default)"
        f"/documents/users/{client.session.uid}/friends"
    )
    uids, page = [], None
    while True:
        url = base + "?pageSize=300" + (f"&pageToken={page}" if page else "")
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8"))
        uids += [d["name"].split("/")[-1] for d in data.get("documents", [])]
        page = data.get("nextPageToken")
        if not page:
            break
    return uids


def list_friends(client: LocketClient) -> list:
    """Lấy đủ bạn bè và tải hồ sơ song song."""
    uids = friend_uids(client)

    def profile(uid):
        try:
            p = client.user.get_profile(uid)
            p["uid"] = uid
            return p
        except Exception:
            return {"uid": uid}

    with ThreadPoolExecutor(max_workers=10) as ex:
        return list(ex.map(profile, uids))


@app.post("/api/login")
def login(email: str = Form(...), password: str = Form(...)):
    try:
        client = get_client(email, password)
        profile = client.user.get_profile()
        return {
            "success": True,
            "uid": client.session.uid,
            "name": f"{profile.get('first_name', '')} {profile.get('last_name', '')}".strip(),
            "username": profile.get("username"),
        }
    except Exception as e:
        handle(e)


@app.get("/api/presets")
def presets():
    """158 mẫu tin nhắn có sẵn của Locket (chữ, icon, màu)."""
    path = os.path.join(os.path.dirname(locketlam.__file__), "caption_presets.json")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    items = [
        {
            "key": k,
            "text": v.get("text", ""),
            "icon": v.get("icon"),
            "colors": v.get("colors", []),
            "text_color": v.get("text_color", "#FFFFFF"),
            "section": v.get("section", ""),
        }
        for k, v in data.items()
        if k != "standard"
    ]
    return {"presets": items}


def ffmpeg_to_h264(data: bytes, max_side: int = 720, crf: int = 28) -> bytes:
    """Đổi bất kỳ video nào (WebM, HEVC...) sang MP4 H.264 mà mọi trình duyệt đều phát được."""
    import subprocess
    import tempfile

    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        raise HTTPException(status_code=501, detail="Máy chủ chưa có ffmpeg (cần gói imageio-ffmpeg trong requirements.txt).")
    with tempfile.TemporaryDirectory() as d:
        src, dst = os.path.join(d, "in.bin"), os.path.join(d, "out.mp4")
        with open(src, "wb") as f:
            f.write(data)
        subprocess.run(
            [exe, "-y", "-i", src, "-t", "15", "-vf", f"scale='min({max_side},iw)':-2", "-c:v", "libx264",
             "-preset", "ultrafast", "-crf", str(crf), "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k",
             "-movflags", "+faststart", dst],
            check=True, capture_output=True, timeout=45,
        )
        with open(dst, "rb") as f:
            return f.read()


def to_mp4(data: bytes) -> bytes:
    """Video đăng lên cần là MP4; WebM (Chrome cũ) thì đổi sang MP4."""
    return data if data[4:8] == b"ftyp" else ffmpeg_to_h264(data)


MEDIA_HOSTS = ("firebasestorage.googleapis.com", "storage.googleapis.com")
CHUNK = 3_500_000  # Vercel giới hạn phản hồi ~4.5MB nên trả từng đoạn (HTTP Range)


@app.get("/api/media")
def media(request: Request, u: str, t: int = 0):
    """
    Phát video của Locket qua máy chủ khi trình duyệt không tự phát được.
    - mặc định: chuyển tiếp (có hỗ trợ Range) để tránh lỗi header/chặn nguồn.
    - t=1: đổi sang H.264 (cho trình duyệt không giải mã được HEVC).
    Chỉ chấp nhận link lưu trữ của Locket.
    """
    p = urlparse(u)
    if p.scheme != "https" or p.hostname not in MEDIA_HOSTS:
        raise HTTPException(status_code=400, detail="Link video không hợp lệ")
    ua = {"User-Agent": "Mozilla/5.0"}
    try:
        if t:
            with urllib.request.urlopen(urllib.request.Request(u, headers=ua), timeout=25) as r:
                src = r.read(8_000_001)
            if len(src) > 8_000_000:
                raise HTTPException(status_code=413, detail="Video quá lớn để chuyển định dạng")
            out = ffmpeg_to_h264(src, max_side=540, crf=30)
            return Response(out, media_type="video/mp4", headers={"Cache-Control": "public, max-age=86400"})

        rng = request.headers.get("range", "")
        if rng.startswith("bytes="):
            a, _, b = rng[6:].partition("-")
            if a:
                start = int(a)
                end = min(int(b), start + CHUNK - 1) if b else start + CHUNK - 1
                up = f"bytes={start}-{end}"
            else:  # đoạn cuối: bytes=-N
                up = f"bytes=-{min(int(b), CHUNK)}"
        else:
            up = f"bytes=0-{CHUNK - 1}"
        req = urllib.request.Request(u, headers={**ua, "Range": up})
        with urllib.request.urlopen(req, timeout=25) as r:
            data, status = r.read(), r.status
            cr, ctype = r.headers.get("Content-Range"), r.headers.get("Content-Type") or ""
        if ctype in ("", "application/octet-stream", "binary/octet-stream"):
            ctype = "video/mp4"
        headers = {"Accept-Ranges": "bytes", "Cache-Control": "public, max-age=3600"}
        if status == 206 and cr:
            headers["Content-Range"] = cr
            return Response(data, status_code=206, media_type=ctype, headers=headers)
        if len(data) > 4_400_000:
            raise HTTPException(status_code=413, detail="Nguồn video không hỗ trợ tải theo đoạn")
        return Response(data, media_type=ctype, headers=headers)
    except HTTPException:
        raise
    except urllib.error.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Nguồn video trả lỗi {e.code}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/upload")
def upload(
    email: str = Form(...),
    password: str = Form(...),
    caption: str = Form(""),
    note_type: str = Form(""),
    icon: str = Form(""),
    colors: str = Form(""),
    text_color: str = Form(""),
    recipients: str = Form(""),
    is_private: bool = Form(False),
    file: UploadFile = File(...),
    video: UploadFile = File(None),
):
    try:
        client = get_client(email, password)
        kw = {"image": file.file.read(), "is_private": is_private}  # với video, `file` là ảnh bìa
        if video is not None:
            kw["video"] = to_mp4(video.file.read())
        if caption:
            kw["caption"] = caption
        if note_type:
            kw["note_type"] = note_type
        if icon:
            kw["icon"] = icon
        cols = [c.strip() for c in colors.split(",") if c.strip()]
        if cols:
            kw["colors"] = cols
        if text_color:
            kw["text_color"] = text_color
        rec = [r.strip() for r in recipients.split(",") if r.strip()]
        if rec and not is_private:
            kw["recipients"] = rec

        moment = client.moments.post(**kw)
        return {"success": True, "id": moment.get("canonical_uid") if isinstance(moment, dict) else None}
    except HTTPException:
        raise
    except Exception as e:
        handle(e)


def _moment_ts(m: dict) -> float:
    d = m.get("date")
    if isinstance(d, dict):
        return d.get("_seconds") or d.get("seconds") or 0
    return 0


def _user_moments(client, uid: str, uids: list, per: int, after=None, pages: int = 1):
    """Moment của MỘT người (cách duy nhất để lấy lịch sử: loại trừ mọi người khác rồi phân trang)."""
    others = [u for u in uids if u != uid]
    out, seen, sync, more = [], set(), after, False
    for _ in range(pages):
        payload = {"should_count_missed_moments": True, "include_group_moments": True, "excluded_users": others}
        if sync:
            payload["sync_token"] = sync
        res = client.post_api("getLatestMomentV2", payload)
        data = res.get("data", []) or []
        new = 0
        for m in data:
            mid = m.get("canonical_uid")
            if mid and mid not in seen:
                seen.add(mid)
                sync = mid
                new += 1
                if m.get("user") == uid:
                    out.append(m)
        if not data or new == 0 or res.get("status") == 304:
            more = False
            break
        more = True
        if len(out) >= per:
            break
    return out, sync, more


@app.post("/api/feed")
def feed(
    email: str = Form(...),
    password: str = Form(...),
    user_uid: str = Form(""),
    limit: int = Form(40),
    after: str = Form(""),
    per: int = Form(3),
):
    """
    - user_uid: chỉ ảnh của người đó, phân trang bằng `after` (sync_token); trả `next`.
    - không có user_uid: gộp `per` ảnh mới nhất của MỖI bạn bè rồi sắp theo thời gian; trả `more`.
    """
    try:
        client = get_client(email, password)
        uids = friend_uids(client)
        if user_uid:
            posts, sync, more = _user_moments(client, user_uid, uids, max(1, min(limit, 100)), after or None, pages=12)
            return {"success": True, "posts": posts, "next": sync if more else None}

        per, limit = max(1, min(per, 30)), max(1, min(limit, 150))

        def one(u):
            try:
                return _user_moments(client, u, uids, per, None, pages=max(1, per // 3 + 1))
            except Exception:
                return [], None, False

        with ThreadPoolExecutor(max_workers=12) as ex:
            results = list(ex.map(one, uids))
        posts, more = [], False
        for out, _sync, m in results:
            posts += out[:per]
            more = more or m or len(out) > per
        posts.sort(key=_moment_ts, reverse=True)
        return {"success": True, "posts": posts[:limit], "more": bool(more)}
    except Exception as e:
        handle(e)


@app.post("/api/friends")
def friends(email: str = Form(...), password: str = Form(...)):
    try:
        client = get_client(email, password)
        return {"success": True, "friends": list_friends(client)}
    except Exception as e:
        handle(e)


@app.post("/api/react")
def react(
    email: str = Form(...),
    password: str = Form(...),
    moment_uid: str = Form(...),
    reaction: str = Form("❤️"),
):
    try:
        client = get_client(email, password)
        client.moments.react(moment_uid=moment_uid, reaction=reaction)
        return {"success": True}
    except Exception as e:
        handle(e)


# ====================== Ảnh, hồ sơ ======================
@app.post("/api/my_moments")
def my_moments(email: str = Form(...), password: str = Form(...), limit: int = Form(30)):
    try:
        client = get_client(email, password)
        return {"success": True, "moments": client.moments.get_all_my_moments(limit=max(1, min(limit, 200)))}
    except Exception as e:
        handle(e)


@app.post("/api/send_message")
def send_message(
    email: str = Form(...), password: str = Form(...),
    receiver_uid: str = Form(...), text: str = Form(""), moment_uid: str = Form(""),
):
    try:
        client = get_client(email, password)
        client.chat.send_message(receiver_uid=receiver_uid, text=text or None, moment_uid=moment_uid or None)
        return {"success": True}
    except Exception as e:
        handle(e)


@app.post("/api/profile")
def profile(email: str = Form(...), password: str = Form(...)):
    try:
        client = get_client(email, password)
        created = None
        try:  # ngày tạo tài khoản lấy từ Firebase Auth
            import requests
            r = requests.post(
                f"{client.session.FIREBASE_AUTH_URL}:lookup?key={client.session.FIREBASE_API_KEY}",
                json={"idToken": client.session.get_valid_token()},
                headers={"User-Agent": client.device.user_agent,
                         "X-Ios-Bundle-Identifier": client.device.bundle_id,
                         "Content-Type": "application/json"},
                timeout=5,
            )
            created = int(r.json()["users"][0].get("createdAt", 0)) or None
        except Exception:
            pass
        return {"success": True, "profile": client.user.get_profile(), "uid": client.session.uid, "created_at_ms": created}
    except Exception as e:
        handle(e)


@app.post("/api/update_name")
def update_name(
    email: str = Form(...), password: str = Form(...),
    first_name: str = Form(...), last_name: str = Form(""),
):
    try:
        client = get_client(email, password)
        client.user.update_name(first_name, last_name)
        return {"success": True}
    except Exception as e:
        handle(e)


@app.post("/api/update_avatar")
def update_avatar(email: str = Form(...), password: str = Form(...), file: UploadFile = File(...)):
    try:
        client = get_client(email, password)
        client.user.update_avatar(file.file.read())
        return {"success": True}
    except Exception as e:
        handle(e)
