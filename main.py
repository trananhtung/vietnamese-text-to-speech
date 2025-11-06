#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ứng dụng Streamlit: Tạo audio tiếng Việt từ text trực tiếp.
"""

import asyncio
import os
import re
import string
import time
import random
import concurrent.futures
import json
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

import streamlit as st

# Suppress warnings về missing ScriptRunContext trong threads
# Đây là cảnh báo bình thường khi chạy background threads trong Streamlit
warnings.filterwarnings("ignore", message=".*missing ScriptRunContext.*")
from pydub import AudioSegment
from gtts import gTTS
import edge_tts

# Windows: dùng event loop policy tương thích cho asyncio khi chạy trong thread
if os.name == "nt":
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass

# -----------------------------
# Tiện ích văn bản
# -----------------------------
SENTENCE_END_RE = re.compile(r"(?<=[.!?。！？…])\s+")
WHITESPACE_RE = re.compile(r"\s+")


def normalize_space(text: str) -> str:
    text = WHITESPACE_RE.sub(" ", text)
    return text.strip()


def normalize_text_preserve_newlines(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [l.strip() for l in text.split("\n")]
    out: List[str] = []
    blank_streak = 0
    for l in lines:
        if l == "":
            blank_streak += 1
            if blank_streak <= 2:
                out.append("")
        else:
            blank_streak = 0
            out.append(l)
    return "\n".join(out).strip()


def split_sentences(text: str) -> List[str]:
    # Newlines được coi như dấu chấm để TTS ngắt nhịp tự nhiên
    # - Nếu trước newline KHÔNG phải là dấu câu, chèn ". "
    # - Nếu trước newline đã là dấu câu, chỉ thay bằng khoảng trắng để tránh ".."
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"(?<![.!?。！？…])\n+", ". ", text)
    text = re.sub(r"\n+", " ", text)
    text = normalize_space(text)
    parts: List[str] = []
    start = 0
    for m in SENTENCE_END_RE.finditer(text + " "):
        end = m.end()
        seg = text[start:end].strip()
        if seg:
            parts.append(seg)
        start = end
    tail = text[start:].strip()
    if tail:
        parts.append(tail)
    if not parts:
        step = 1000
        parts = [text[i : i + step] for i in range(0, len(text), step)]
    return parts


def chunk_for_tts(sentences: List[str], max_chars: int = 2200) -> List[str]:
    """Gom câu thành các mảnh an toàn cho dịch vụ (edge-tts ổn định <~2400 ký tự)."""
    chunks: List[str] = []
    buf: List[str] = []
    cur = 0
    for s in sentences:
        if not s:
            continue
        if cur + len(s) + 1 > max_chars and buf:
            chunks.append(" ".join(buf))
            buf = [s]
            cur = len(s)
        else:
            buf.append(s)
            cur += len(s) + 1
    if buf:
        chunks.append(" ".join(buf))
    return chunks


# -----------------------------
# Backend TTS
# -----------------------------
@dataclass
class TTSConfig:
    backend: str = "edge-tts"  # "edge-tts" | "gtts"
    voice: str = "vi-VN-HoaiMyNeural"
    rate: str = "-10%"
    pitch: str = "-20Hz"
    max_retries: int = 3           # số lần thử lại khi lỗi mạng/no audio
    retry_delay: float = 1.5       # giây, delay ban đầu cho backoff
    throttle: float = 2          # giãn cách giữa các mảnh
    allow_fallback: bool = True    # fallback sang gTTS nếu edge-tts lỗi nhiều lần


class TTSBackend:
    def __init__(self, cfg: TTSConfig):
        self.cfg = cfg

    def synth_to_file(self, text: str, outfile: Path) -> None:
        raise NotImplementedError

    def synth_chunks_to_file(self, chunks: List[str], outfile: Path) -> None:
        """Synth từng mảnh → ghép lại thành 1 MP3 tạm ở `outfile`."""
        tmp_files: List[Path] = []
        try:
            for idx, ch in enumerate(chunks, 1):
                ch = ch.strip()
                if not ch:
                    continue
                tmp = outfile.with_suffix("").with_name(outfile.stem + f"__part{idx}.mp3")
                self.synth_to_file(ch, tmp)
                # Xác thực file có dữ liệu
                if not tmp.exists() or tmp.stat().st_size < 800:
                    raise RuntimeError("Audio part rỗng sau synth.")
                tmp_files.append(tmp)
                # throttle nhẹ để tránh rate limit
                if self.cfg.throttle > 0:
                    time.sleep(self.cfg.throttle)
            # Ghép
            audio: Optional[AudioSegment] = None
            for p in tmp_files:
                seg = AudioSegment.from_file(p, format="mp3")
                audio = seg if audio is None else audio + seg
            if audio is None:
                raise RuntimeError("Không tạo được audio từ các phần!")
            audio.export(outfile, format="mp3", bitrate="192k")
        finally:
            for p in tmp_files:
                try:
                    p.unlink(missing_ok=True)
                except Exception:
                    pass


class EdgeTTSBackend(TTSBackend):
    def synth_to_file(self, text: str, outfile: Path) -> None:
        async def run_once() -> None:
            communicate = edge_tts.Communicate(text, voice=self.cfg.voice, rate=self.cfg.rate, pitch=self.cfg.pitch)
            await communicate.save(str(outfile))

        # Thử nhiều lần + backoff + jitter
        last_err: Optional[Exception] = None
        for attempt in range(self.cfg.max_retries + 1):
            try:
                asyncio.run(run_once())
                # kiểm tra file hợp lệ
                if outfile.exists() and outfile.stat().st_size >= 800:
                    return
                raise RuntimeError("no audio received (file rỗng)")
            except Exception as e:
                last_err = e
                if attempt < self.cfg.max_retries:
                    base = self.cfg.retry_delay * (2 ** attempt)
                    jitter = random.uniform(0, 0.5)
                    time.sleep(base + jitter)
                else:
                    break
        # Fallback nếu cho phép
        if self.cfg.allow_fallback:
            try:
                tts = gTTS(text=text, lang="vi", tld="com.vn")
                tts.save(str(outfile))
                if outfile.exists() and outfile.stat().st_size >= 800:
                    return
            except Exception:
                pass
        # Nếu đến đây vẫn lỗi → ném lỗi cuối cùng
        raise last_err or RuntimeError("edge-tts: no audio received")


class GTTSBackend(TTSBackend):
    def synth_to_file(self, text: str, outfile: Path) -> None:
        tts = gTTS(text=text, lang="vi", tld="com.vn")
        tts.save(str(outfile))


def build_backend(cfg: TTSConfig) -> TTSBackend:
    if cfg.backend == "edge-tts":
        return EdgeTTSBackend(cfg)
    return GTTSBackend(cfg)


# -----------------------------
# Persist & Utilities
# -----------------------------
_PATH_MAP_FILE = Path.cwd() / "path_map.json"
_RESUME_FILE = Path.cwd() / "resume_state.json"


def _json_load(path: Path):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _json_save(path: Path, data: dict):
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


SAFE_CHARS = f"-_() {string.ascii_letters}{string.digits}đĐ_"


def sanitize_filename(name: str) -> str:
    name = normalize_space(name)
    cleaned = []
    for ch in name:
        cleaned.append(ch if ch in SAFE_CHARS else "_")
    s = "".join(cleaned).strip("._ ")
    return s[:120] if len(s) > 120 else s


# -----------------------------
# Streamlit App
# -----------------------------
def init_session_state():
    """Khởi tạo session state nếu chưa có."""
    defaults = {
        "items": [],
        "current_iid": None,
        "input_path": "",
        "output_dir": str(Path.cwd() / "output_audio"),
        "processing": False,
        "stop_processing": False,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def source_key(input_path: str, output_dir: str, audio_fmt: str) -> str:
    """Tạo key duy nhất cho nguồn."""
    return f"{input_path.strip()}||{output_dir.strip()}||{audio_fmt}"


def load_resume_store() -> dict:
    """Tải resume state từ file."""
    data = _json_load(_RESUME_FILE)
    # migrate từ dạng cũ (1 nguồn) sang đa nguồn nếu cần
    if data and isinstance(data, dict) and "sources" not in data and {
        "input_path", "output_dir", "audio_fmt", "current_iid", "position_ms"
    }.issubset(set(data.keys())):
        key = f"{data.get('input_path', '')}||{data.get('output_dir', '')}||{data.get('audio_fmt', 'mp3')}"
        data = {
            "sources": {
                key: {
                    "current_iid": data.get("current_iid"),
                    "position_ms": int(data.get("position_ms", 0)),
                    "updated_at": int(time.time()),
                }
            },
            "last_source": key,
        }
        _json_save(_RESUME_FILE, data)
    if not isinstance(data, dict):
        data = {}
    data.setdefault("sources", {})
    return data


def save_resume_state(input_path: str, output_dir: str, audio_fmt: str, current_iid: Optional[int], position_ms: int = 0):
    """Lưu resume state."""
    key = source_key(input_path, output_dir, audio_fmt)
    store = load_resume_store()
    store.setdefault("sources", {})[key] = {
        "current_iid": current_iid,
        "position_ms": int(max(0, position_ms)),
        "updated_at": int(time.time()),
    }
    store["last_source"] = key
    _json_save(_RESUME_FILE, store)


def get_resume_state(input_path: str, output_dir: str, audio_fmt: str) -> Optional[dict]:
    """Lấy resume state cho nguồn hiện tại."""
    store = load_resume_store()
    key = source_key(input_path, output_dir, audio_fmt)
    return store.get("sources", {}).get(key)


def load_path_map() -> dict:
    """Tải path mapping."""
    return _json_load(_PATH_MAP_FILE)


def save_path_map(path_map: dict):
    """Lưu path mapping."""
    _json_save(_PATH_MAP_FILE, path_map)


def main():
    st.set_page_config(
        page_title="🎧 Tạo Audio Tiếng Việt",
        page_icon="🎧",
        layout="wide",
        initial_sidebar_state="expanded"
    )
    
    st.title("🎧 Tạo Audio Tiếng Việt từ Văn bản")
    st.caption("Chuyển đổi text thành audio tiếng Việt với TTS, kèm trình đọc và tự động tiếp tục")
    
    init_session_state()
    
    # Sidebar - Cấu hình
    with st.sidebar:
        st.header("⚙️ Cấu hình")
        
        # Nguồn - chỉ hỗ trợ nhập text trực tiếp
        st.subheader("📁 Nguồn")
        st.info("💡 Nhập nội dung text trực tiếp vào trình soạn thảo bên dưới")
        st.session_state.input_path = "direct_text_input"
        
        # Input tên cho text/audio đã được loại bỏ. Ứng dụng sẽ dùng tên mặc định.
        
        # Thư mục xuất
        output_dir = st.text_input(
            "Thư mục lưu file audio:",
            value=st.session_state.output_dir,
            help="Đường dẫn thư mục trên máy tính nơi các file audio sẽ được lưu. Ví dụ: ~/Documents/audio hoặc ./output"
        )
        st.session_state.output_dir = output_dir
        
        # Mặc định: bỏ qua file đã có và lưu mối liên kết (không cần hiển thị trên UI)
        skip_existing = True
        
        # TTS Config
        st.subheader("🎤 Giọng đọc (TTS)")
        backend = st.selectbox("Công cụ TTS:", ["edge-tts", "gtts"], index=0, key="tts_backend", 
                               help="edge-tts: Microsoft Edge TTS (chất lượng cao, cần internet)\ngtts: Google TTS (đơn giản, miễn phí)")
        voice = st.selectbox(
            "Giọng đọc:",
            ["vi-VN-HoaiMyNeural", "vi-VN-NamMinhNeural", "vi-VN-HuongHanhNeural"],
            index=0,
            key="tts_voice",
            help="Chọn giọng đọc tiếng Việt (chỉ áp dụng cho edge-tts)"
        )
        col1, col2 = st.columns(2)
        with col1:
            rate = st.text_input("Tốc độ đọc:", value="-10%", key="tts_rate",
                                help="Điều chỉnh tốc độ đọc. Ví dụ: +20% (nhanh hơn), -10% (chậm hơn), +0% (mặc định)")
        with col2:
            pitch = st.text_input("Cao độ giọng:", value="-20Hz", key="tts_pitch",
                                 help="Điều chỉnh cao độ giọng. Ví dụ: +50Hz (cao hơn), -20Hz (thấp hơn), +0Hz (mặc định)")
        
        # Thông số độ bền
        st.subheader("🔧 Thông số xử lý")
        retries = st.number_input("Số lần thử lại:", min_value=0, max_value=6, value=3, key="tts_retries",
                                 help="Số lần thử lại khi gặp lỗi mạng hoặc không nhận được audio")
        retry_delay = st.number_input("Khoảng chờ giữa các lần thử (giây):", min_value=0.0, value=1.5, step=0.1, key="tts_retry_delay",
                                     help="Thời gian chờ trước khi thử lại (tự động tăng dần theo số lần thử)")
        throttle = st.number_input("Khoảng cách giữa các đoạn (giây):", min_value=0.0, value=2.0, step=0.1, key="tts_throttle",
                                   help="Thời gian chờ giữa các đoạn text để tránh bị giới hạn tốc độ từ dịch vụ TTS")
        max_workers = st.number_input(
            "Số luồng xử lý tối đa:",
            min_value=1,
            max_value=max(1, os.cpu_count() or 4),
            value=min(3, os.cpu_count() or 2),
            key="tts_max_workers",
            help=f"Số chương có thể xử lý đồng thời (CPU của bạn: {os.cpu_count() or 2} cores). Tăng số này để xử lý nhanh hơn nhưng tốn nhiều tài nguyên hơn."
        )
        
        # Định dạng audio
        st.subheader("🎵 Xuất file âm thanh")
        audio_format = st.selectbox("Định dạng file âm thanh:", ["mp3", "ogg", "m4a"], index=0, key="audio_format",
                                   help="mp3: Định dạng phổ biến nhất, tương thích với hầu hết thiết bị\nogg: Chất lượng tốt, kích thước file nhỏ hơn\nm4a: Tương thích tốt với thiết bị Apple (iPhone, iPad, Mac)")
        
        # Hiển thị bitrate phù hợp với format đã chọn
        if audio_format == "mp3":
            st.info("💡 MP3 sẽ tự động sử dụng chất lượng cao nhất (VBR). Không cần cài đặt bitrate.")
            # Giữ giá trị bitrate trong session_state để không mất khi chuyển format
            if "audio_bitrate" not in st.session_state:
                st.session_state.audio_bitrate = "192k"
            bitrate = st.session_state.audio_bitrate
        else:
            # Hiển thị trường bitrate cho OGG/M4A
            default_bitrate = st.session_state.get("audio_bitrate", "192k")
            bitrate = st.text_input(
                f"Chất lượng âm thanh ({audio_format.upper()}):", 
                value=default_bitrate, 
                key="audio_bitrate",
                help=f"Tốc độ bit cho {audio_format.upper()}. Số càng cao = chất lượng càng tốt nhưng file càng lớn.\nVí dụ: 128k (chất lượng thấp), 192k (chất lượng tốt), 256k (chất lượng cao), 320k (chất lượng rất cao)"
            )
        
        # Tùy chọn phát
        st.subheader("▶️ Cài đặt phát âm thanh")
        autoplay_next = st.checkbox(
            "Tự động phát chương tiếp theo", 
            value=False,
            help="Khi bật, sau khi chương hiện tại phát xong sẽ tự động chuyển sang và phát chương kế tiếp"
        )
        resume_enabled = st.checkbox(
            "Ghi nhớ vị trí đang nghe", 
            value=True,
            help="Khi bật, ứng dụng sẽ nhớ vị trí bạn đã dừng nghe và tiếp tục phát từ đó khi mở lại sau"
        )
    
    # Main area
    col1, col2 = st.columns([1, 2])
    
    with col1:
        st.subheader("📋 Thông tin")
        
        # Nút áp dụng text
        is_processing = st.session_state.get("processing", False)
        if st.button("✅ Áp dụng text đã nhập", type="primary", use_container_width=True, disabled=is_processing):
            if not is_processing:
                scan_items("direct_text")
        
        # Hiển thị thông tin nếu đã có items
        items = st.session_state.get("items", [])
        if items and isinstance(items, list) and len(items) > 0:
            # Vì chỉ có 1 item, không cần selectbox
            st.session_state.current_iid = 1
            
            title, text = items[0]
            char_count = len(text)
            edited_key = f"edited_text_1"
            is_edited = edited_key in st.session_state and st.session_state[edited_key] != text
            
            # Hiển thị thông tin
            if is_edited:
                st.info(f"📄 **{title}**\n\n📊 {char_count:,} ký tự ✏️ đã chỉnh sửa")
            else:
                st.info(f"📄 **{title}**\n\n📊 {char_count:,} ký tự")
            
            # Nút tạo audio - chỉ 1 nút
            is_processing = st.session_state.get("processing", False)
            if st.button("🎵 Tạo audio", type="primary", use_container_width=True,
                        help="Tạo file audio từ text đã nhập", disabled=is_processing):
                if not is_processing:
                    process_audio([1], skip_existing, backend, voice, rate, pitch, 
                                retries, retry_delay, throttle, max_workers, audio_format, bitrate, output_dir)
            
            # Nút mở thư mục xuất
            if st.button("📂 Mở thư mục chứa file audio", use_container_width=True,
                        help="Mở thư mục chứa các file audio đã tạo trên máy tính của bạn"):
                try:
                    out = Path(output_dir).expanduser().resolve()
                    out.mkdir(parents=True, exist_ok=True)
                    import sys
                    import subprocess
                    if os.name == "nt":
                        os.startfile(str(out))
                    elif sys.platform == "darwin":
                        subprocess.Popen(["open", str(out)])
                    else:
                        subprocess.Popen(["xdg-open", str(out)])
                    st.success(f"✅ Đã mở thư mục: {out}")
                except Exception as e:
                    st.error(f"❌ Không mở được thư mục: {e}")
        else:
            st.info("💡 Chưa có dữ liệu. Hãy nhập text vào trình soạn thảo bên dưới và bấm nút 'Áp dụng text đã nhập' ở trên.")
    
    with col2:
        st.subheader("📖 Nội dung")
        
        # Hiển thị trình soạn thảo nếu chưa có items
        if not st.session_state.get("items") or len(st.session_state.get("items", [])) == 0:
            st.markdown("### ✍️ Trình soạn thảo văn bản")
            
            # Lấy text từ session_state hoặc default
            default_text = st.session_state.get("direct_text_input", "")
            
            # Text editor lớn hơn cho việc nhập text
            is_processing = st.session_state.get("processing", False)
            edited_text = st.text_area(
                "Nhập hoặc dán nội dung văn bản của bạn:",
                value=default_text,
                height=400,
                key="direct_text_editor",
                help="Nhập hoặc dán nội dung văn bản bạn muốn chuyển thành audio. Sau đó bấm 'Áp dụng text đã nhập' ở bên trái để bắt đầu.",
                disabled=is_processing
            )
            
            # Lưu vào session_state
            st.session_state.direct_text_input = edited_text
            
            # Hiển thị thống kê
            char_count = len(edited_text)
            word_count = len(edited_text.split()) if edited_text else 0
            st.caption(f"📊 Thống kê: {char_count:,} ký tự | {word_count:,} từ")
            
            # Hướng dẫn
            if not edited_text.strip():
                st.info("💡 **Hướng dẫn:** Nhập hoặc dán nội dung văn bản vào ô trên, sau đó bấm nút '✅ Áp dụng text đã nhập' ở bên trái để bắt đầu tạo audio.")
            else:
                estimated_time = char_count / 10  # Ước tính thời gian (giả sử ~10 ký tự/giây)
                st.success(f"✅ Đã nhập {char_count:,} ký tự ({word_count:,} từ). Ước tính thời gian audio: ~{estimated_time/60:.1f} phút. Bấm 'Áp dụng text đã nhập' để tiếp tục.")
            
            # Không hiển thị phần còn lại
            st.stop()
        
        items = st.session_state.get("items", [])
        if items and isinstance(items, list) and len(items) > 0:
            # Vì chỉ có 1 item, đơn giản hóa logic
            title, original_text = items[0]
            
            st.markdown(f"### {title}")
            
            # Text editor cho phép chỉnh sửa
            edited_key = "edited_text_1"
            
            # Khởi tạo giá trị nếu chưa có
            if edited_key not in st.session_state:
                st.session_state[edited_key] = original_text
            
            # Lấy giá trị hiện tại
            display_text = st.session_state[edited_key]
            
            # Nếu original_text đã thay đổi (từ scan_items), cập nhật lại
            current_title, current_text = items[0]
            if current_title != title or current_text != original_text:
                # Items đã thay đổi, cập nhật lại
                st.session_state[edited_key] = current_text
                display_text = current_text
                original_text = current_text
                title = current_title
            
            is_processing = st.session_state.get("processing", False)
            edited_text = st.text_area(
                "Nội dung (có thể chỉnh sửa):", 
                value=display_text, 
                height=300, 
                key=edited_key,
                help="Bạn có thể chỉnh sửa nội dung trước khi tạo audio. Nội dung đã chỉnh sửa sẽ được lưu tự động và sử dụng khi tạo audio.",
                disabled=is_processing
            )
            
            # Lưu nội dung đã chỉnh sửa vào items
            if edited_text != original_text:
                items[0] = (title, edited_text)
                st.session_state.items = items
                
                # Hiển thị thông tin thay đổi
                diff = len(edited_text) - len(original_text)
                if diff > 0:
                    st.info(f"💾 Đã chỉnh sửa: Thêm {diff} ký tự. Audio sẽ sử dụng nội dung đã chỉnh sửa.")
                elif diff < 0:
                    st.info(f"💾 Đã chỉnh sửa: Xóa {abs(diff)} ký tự. Audio sẽ sử dụng nội dung đã chỉnh sửa.")
                else:
                    st.info("💾 Đã chỉnh sửa nội dung. Audio sẽ sử dụng nội dung đã chỉnh sửa.")
            elif edited_text == original_text:
                # Nếu người dùng khôi phục về nội dung gốc
                items[0] = (title, original_text)
                st.session_state.items = items
                
            # Audio player
            # Chỉ hiển thị nếu đã tạo audio thành công cho text hiện tại
            audio_path = None
            if "last_generated_audio_path" in st.session_state:
                audio_path = Path(st.session_state.last_generated_audio_path)
                if not audio_path.exists():
                    audio_path = None
            
            if audio_path and audio_path.exists():
                st.subheader("🎵 Phát âm thanh")
                with open(audio_path, "rb") as f:
                    audio_bytes = f.read()
                
                # Lấy resume position nếu có
                start_time = 0
                if resume_enabled:
                    resume_state = get_resume_state(
                        st.session_state.input_path,
                        output_dir,
                        audio_format
                    )
                    if resume_state and resume_state.get("current_iid") == 1:
                        start_time = resume_state.get("position_ms", 0) // 1000
                
                st.audio(audio_bytes, start_time=start_time)
                
                # Thông tin file và nút download
                file_size = audio_path.stat().st_size
                file_size_mb = file_size / (1024 * 1024)
                
                col_info, col_download = st.columns([2, 1])
                with col_info:
                    st.caption(f"📁 Tên file: {audio_path.name} | Kích thước: {file_size_mb:.2f} MB")
                with col_download:
                    st.download_button(
                        label="⬇️ Tải về máy",
                        data=audio_bytes,
                        file_name=audio_path.name,
                        mime=f"audio/{audio_format}" if audio_format != "m4a" else "audio/mp4",
                        use_container_width=True,
                        key="download_main",
                        help="Tải file audio về máy tính của bạn"
                    )
                
                # Lưu resume state
                if resume_enabled:
                    save_resume_state(
                        st.session_state.input_path,
                        output_dir,
                        audio_format,
                        1,
                        start_time * 1000
                    )
            else:
                st.info("💡 Chưa có file audio. Hãy bấm nút '🎵 Tạo audio' ở bên trái để tạo file audio.")
    
    # Status và progress
    if "processing_status" in st.session_state:
        st.info(st.session_state.processing_status)
    if "processing_progress" in st.session_state:
        st.progress(st.session_state.processing_progress / 100.0)


def scan_items(input_mode_flag):
    """Quét nội dung từ text trực tiếp."""
    try:
        direct_text = st.session_state.get("direct_text_input", "").strip()
        if not direct_text:
            st.error("Vui lòng nhập nội dung text trước.")
            return
        
        # Lấy tên từ input hoặc dùng mặc định
        title = st.session_state.get("text_title", "Text đã nhập").strip()
        if not title:
            title = "Text đã nhập"
        
        # Tạo 1 item duy nhất cho toàn bộ text
        items = [(title, normalize_text_preserve_newlines(direct_text))]
        
        # Lưu items vào session_state
        st.session_state.items = items
        st.session_state.current_iid = 1
        
        # Xóa các chỉnh sửa cũ khi quét lại
        keys_to_delete = [key for key in st.session_state.keys() if key.startswith("edited_text_")]
        for key in keys_to_delete:
            del st.session_state[key]
        
        # Xóa đường dẫn file audio cũ khi quét lại text mới
        if "last_generated_audio_path" in st.session_state:
            del st.session_state.last_generated_audio_path
        
        # Thông báo thành công
        st.success(f"✅ Đã tạo 1 item từ text đã nhập: '{title}' ({len(items[0][1])} ký tự).")
        
        st.rerun()
    except Exception as e:
        st.error(f"Lỗi khi quét nội dung: {e}")


def generate_audio_filename(text: str, audio_format: str) -> str:
    """Tạo tên file audio từ đoạn text đầu tiên + random text."""
    # Lấy khoảng 80 ký tự đầu tiên, loại bỏ khoảng trắng thừa
    first_text = text.strip()[:80]
    first_text = " ".join(first_text.split())  # Chuẩn hóa khoảng trắng
    
    # Tạo random suffix (8 ký tự hex)
    random_suffix = ''.join(random.choices('0123456789abcdef', k=8))
    
    # Sanitize để làm tên file hợp lệ
    safe_text = sanitize_filename(first_text)
    
    # Giới hạn độ dài để tránh tên file quá dài
    if len(safe_text) > 60:
        safe_text = safe_text[:60]
    
    # Tạo tên file: text_đầu_tiên_random.extension
    return f"{safe_text}_{random_suffix}.{audio_format}"


def get_expected_audio_path(iid: int, audio_format: str, output_dir: str) -> Path:
    """Tính đường dẫn file audio dự kiến (để hiển thị, không dùng để check tồn tại nữa)."""
    items = st.session_state.get("items", [])
    if not items or not isinstance(items, list) or iid < 1 or iid > len(items):
        return Path("/dev/null")
    _, text = items[iid - 1]
    filename = generate_audio_filename(text, audio_format)
    out = Path(output_dir).expanduser().resolve()
    return out / filename


def process_audio(ids: List[int], skip_existing: bool, backend: str, voice: str,
                 rate: str, pitch: str, retries: int, retry_delay: float, throttle: float,
                 max_workers: int, audio_format: str, bitrate: str, output_dir: str):
    """Xử lý tạo audio cho các chương được chọn."""
    items = st.session_state.get("items", [])
    if not items or not isinstance(items, list) or len(items) == 0:
        st.error("Chưa có dữ liệu. Hãy quét nội dung trước.")
        return
    
    out = Path(output_dir).expanduser().resolve()
    try:
        out.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        st.error(f"Không tạo được thư mục xuất: {e}")
        return
    
    cfg = TTSConfig(
        backend=backend,
        voice=voice,
        rate=rate,
        pitch=pitch,
        max_retries=max(0, retries),
        retry_delay=max(0.0, retry_delay),
        throttle=max(0.0, throttle),
        allow_fallback=True,
    )
    
    def export_merge(outfile_base: Path, audio: AudioSegment) -> Path:
        if audio_format == "mp3":
            final = outfile_base.with_suffix(".mp3")
            # MP3 VBR chất lượng 0 (tốt nhất): ffmpeg → -q:a 0
            audio.export(final, format="mp3", parameters=["-q:a", "0"])
            return final
        elif audio_format == "ogg":
            final = outfile_base.with_suffix(".ogg")
            audio.export(final, format="ogg", bitrate=bitrate)
            return final
        else:  # m4a (aac)
            final = outfile_base.with_suffix(".m4a")
            audio.export(final, format="ipod", bitrate=bitrate)
            return final
    
    def process_one(iid: int, items_ref: List[Tuple[str, str]], stop_event_ref: dict) -> Tuple[int, str]:
        if stop_event_ref.get("stop", False):
            return iid, "stopped"
        
        if iid < 1 or iid > len(items_ref):
            return iid, "Lỗi: ID không hợp lệ"
        title, text = items_ref[iid - 1]
        
        # Tạo tên file mới mỗi lần dựa trên text đầu tiên + random
        filename = generate_audio_filename(text, audio_format)
        target_path = out / filename
        
        # Đảm bảo không trùng tên (nếu trùng thì thêm random lại)
        while target_path.exists():
            # Tạo lại với random mới
            filename = generate_audio_filename(text, audio_format)
            target_path = out / filename
        
        try:
            backend_inst = build_backend(cfg)
            chunks = chunk_for_tts(split_sentences(text))
            # Dùng tên file tạm dựa trên target_path
            tmp_mp3 = target_path.with_suffix(".mp3").with_name(target_path.stem + "__tmp_merge.mp3")
            backend_inst.synth_chunks_to_file(chunks, tmp_mp3)
            audio = AudioSegment.from_file(tmp_mp3, format="mp3")
            # Thêm 5 giây im lặng ở đầu trước khi xuất
            try:
                pre_silence = AudioSegment.silent(duration=2000, frame_rate=audio.frame_rate)
                pre_silence = pre_silence.set_channels(audio.channels).set_sample_width(audio.sample_width)
                audio = pre_silence + audio
            except Exception:
                # Nếu tạo silence gặp lỗi vì khác tham số, fallback dùng mặc định
                audio = AudioSegment.silent(duration=2000) + audio
            final_path = export_merge(target_path.with_suffix(""), audio)
            try:
                tmp_mp3.unlink(missing_ok=True)
            except Exception:
                pass
            return iid, f"Xong: {final_path.name}"
        except Exception as e:
            return iid, f"Lỗi: {e}"
    
    # Xử lý với progress bar
    st.session_state.processing = True
    stop_event = {"stop": False}  # Dùng dict để có thể thay đổi từ thread
    
    total = len(ids)
    completed = 0
    
    # Không cần check file tồn tại vì mỗi lần tạo file mới với tên random
    remaining: List[int] = ids.copy()
    
    # Hiển thị progress
    progress_placeholder = st.empty()
    status_placeholder = st.empty()
    
    # Nút dừng
    stop_placeholder = st.empty()
    if stop_placeholder.button("⏹️ Dừng xử lý", key="stop_processing_btn"):
        stop_event["stop"] = True
    
    try:
        # Xử lý các file còn lại với ThreadPoolExecutor
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Truyền items và stop_event vào hàm process_one
            future_map = {
                executor.submit(process_one, iid, items, stop_event): iid 
                for iid in remaining
            }
            
            # Dict để lưu đường dẫn file audio đã tạo
            generated_files = {}
            
            for future in concurrent.futures.as_completed(future_map):
                if stop_event["stop"]:
                    break
                iid, msg = future.result()
                completed += 1
                progress = completed / total
                progress_placeholder.progress(progress)
                status_placeholder.info(f"[{completed}/{total}] {msg}")
                st.session_state.processing_status = f"[{completed}/{total}] {msg}"
                st.session_state.processing_progress = progress * 100
                
                # Lưu đường dẫn file đã tạo (nếu thành công)
                if "Xong:" in msg and iid == 1:
                    # Trích xuất tên file từ message
                    filename = msg.replace("Xong: ", "")
                    file_path = out / filename
                    if file_path.exists():
                        generated_files[iid] = str(file_path)
    
    finally:
        st.session_state.processing = False
        stop_placeholder.empty()
        
        # Lưu đường dẫn file audio đã tạo vào session_state
        if generated_files.get(1):
            st.session_state.last_generated_audio_path = generated_files[1]
        
        if stop_event["stop"]:
            st.warning("Đã dừng theo yêu cầu.")
        else:
            st.success(f"Hoàn tất {completed}/{total} chương. Bạn có thể chọn chương và phát audio.")
        
        progress_placeholder.empty()
        status_placeholder.empty()
        
        if "processing_status" in st.session_state:
            del st.session_state.processing_status
        if "processing_progress" in st.session_state:
            del st.session_state.processing_progress


if __name__ == "__main__":
    main()

