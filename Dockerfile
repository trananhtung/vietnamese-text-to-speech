# Sử dụng Python 3.12 slim image
FROM python:3.12-slim

# Đặt thư mục làm việc
WORKDIR /app

# Cài đặt ffmpeg và các dependencies hệ thống
RUN apt-get update && apt-get install -y \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Copy file requirements
COPY requirements.txt .

# Cài đặt Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY main.py .

# Tạo thư mục cho output và temp files
RUN mkdir -p /app/output_audio /app/temp_uploads

# Expose port Streamlit
EXPOSE 8501

# Health check - kiểm tra port có mở không
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import socket; s=socket.socket(); s.connect(('localhost', 8501)); s.close()" || exit 1

# Chạy Streamlit
CMD ["streamlit", "run", "main.py", "--server.port=8501", "--server.address=0.0.0.0"]

