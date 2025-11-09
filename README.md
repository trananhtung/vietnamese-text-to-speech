# 🎧 Tạo Audio Tiếng Việt từ Văn bản

App convert text sang audio tiếng Việt. Hỗ trợ nhập text trực tiếp hoặc xử lý hàng loạt file .txt từ folder.

![Giao diện ứng dụng](demo.png)

## 🎵 Demo Audio

Nghe thử chất lượng audio được tạo từ app:

<audio controls>
  <source src="demo-audio.mp3" type="audio/mpeg">
  Trình duyệt của bạn không hỗ trợ audio player. <a href="demo-audio.mp3">Tải về</a> để nghe.
</audio>

## Tính năng

- Nhập text trực tiếp hoặc quét folder chứa file .txt
- Chỉnh sửa text trước khi tạo audio
- Phát audio ngay trong app
- Xử lý đa luồng (nhiều file cùng lúc)
- Hỗ trợ MP3/OGG/M4A
- MP3 dùng VBR chất lượng cao (~245-270 kbps)
- Retry/backoff, throttle, fallback gTTS khi lỗi

## Cài đặt

### Local

**Yêu cầu:**
- Python 3.8+
- ffmpeg

**Cài đặt Python packages:**
```bash
pip install -r requirements.txt
```

Hoặc cài thủ công:
```bash
pip install streamlit pydub gTTS edge-tts
```

**Cài đặt ffmpeg:**

- **Windows**: 
  - Tải từ [ffmpeg.org](https://ffmpeg.org/download.html)
  - Giải nén và thêm vào PATH
  - Hoặc dùng chocolatey: `choco install ffmpeg`

- **macOS**: 
  ```bash
  brew install ffmpeg
  ```

- **Linux**: 
  ```bash
  # Ubuntu/Debian
  sudo apt-get install ffmpeg
  
  # Fedora
  sudo dnf install ffmpeg
  
  # Arch
  sudo pacman -S ffmpeg
  ```

**Chạy app:**
```bash
streamlit run main.py
```

Mở http://localhost:8501

### Docker

**Yêu cầu:**
- Docker đã cài đặt
- Docker Compose (tùy chọn)

**Cài đặt Docker:**
- Xem hướng dẫn: https://docs.docker.com/engine/install/

**Build và chạy:**

Cách 1 - Docker command:
```bash
# Build image
docker build -t tao-audio-viet .

# Chạy container
docker run -d \
  --name tao-audio-viet \
  -p 8501:8501 \
  -v $(pwd)/output_audio:/app/output_audio \
  -v $(pwd)/temp_uploads:/app/temp_uploads \
  tao-audio-viet
```

Cách 2 - Docker Compose (khuyến nghị):
```bash
docker-compose up -d
```

**Truy cập:**
- Mở http://localhost:8501

**Dừng container:**
```bash
# Docker command
docker stop tao-audio-viet
docker rm tao-audio-viet

# Docker Compose
docker-compose down
```

**Lưu ý:**
- Volume mount (`-v`) để lưu file audio ra ngoài container
- File audio lưu trong thư mục `output_audio` trên máy host

## Cấu hình

Tất cả cấu hình trong sidebar:

**TTS:**
- `edge-tts` (mặc định) - chất lượng cao, cần internet
- `gtts` - đơn giản, miễn phí

**Giọng đọc (edge-tts):**
- `vi-VN-HoaiMyNeural` (nữ)
- `vi-VN-NamMinhNeural` (nam) - mặc định
- `vi-VN-HuongHanhNeural` (nữ)

**Điều chỉnh:**
- Tốc độ: `+20%` (nhanh), `-10%` (chậm), `+0%` (mặc định)
- Cao độ: `+50Hz` (cao), `-20Hz` (thấp), `+0Hz` (mặc định)

**Xử lý:**
- Số lần retry: 3 (mặc định)
- Retry delay: 1.5s (mặc định)
- Throttle: 2s (mặc định)
- Max workers: 3 (mặc định)

**Format:**
- MP3: VBR tự động (chất lượng cao)
- OGG/M4A: tùy chỉnh bitrate

## Sử dụng

### Mode 1: Nhập text trực tiếp

1. Sidebar → chọn "Nhập text trực tiếp"
2. Nhập/dán text vào editor bên phải
3. Bấm "🎵 Tạo audio" bên trái
4. Chỉnh sửa text nếu cần (tự động lưu)
5. Nghe/tải audio sau khi tạo xong

### Mode 2: Folder chứa file .txt

1. Sidebar → chọn "Chọn folder chứa file .txt"
2. Nhập đường dẫn folder (vd: `~/Documents/texts`)
3. Bấm "🔍 Quét folder"
4. Bấm "🚀 Start" để convert tất cả
5. Xem progress và kết quả
6. Bấm "📂 Mở thư mục chứa file audio" để xem files

**Lưu ý:**
- File audio lưu vào thư mục cấu hình (mặc định: `output_audio`)
- Tên file tự động từ tên .txt (folder mode) hoặc từ text (direct mode)
- Có thể chỉnh sửa text trước khi tạo audio
