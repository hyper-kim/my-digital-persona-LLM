#!/bin/bash
# Omni-Brain GCP VM 자동 셋업 스크립트
# 실행: nohup bash vm_setup.sh > /home/kjy/setup.log 2>&1 &
set -e
LOG=/home/kjy/setup.log
echo "[$(date)] === Omni-Brain VM 셋업 시작 ===" | tee -a $LOG

# ── 1. NVIDIA 드라이버 + CUDA 설치 ───────────────────────────────────────────
echo "[$(date)] NVIDIA 드라이버 설치 중..." | tee -a $LOG
sudo apt-get update -qq
sudo apt-get install -y linux-headers-$(uname -r) 2>&1 | tail -3

# CUDA 12.x repo 등록
wget -q https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2204/x86_64/cuda-keyring_1.1-1_all.deb
sudo dpkg -i cuda-keyring_1.1-1_all.deb
sudo apt-get update -qq
sudo apt-get install -y cuda-drivers 2>&1 | tail -5
echo "[$(date)] NVIDIA 드라이버 설치 완료" | tee -a $LOG

# ── 2. Ollama 설치 ────────────────────────────────────────────────────────────
echo "[$(date)] Ollama 설치 중..." | tee -a $LOG
curl -fsSL https://ollama.com/install.sh | sh 2>&1 | tail -3

# 외부 접속 허용
sudo mkdir -p /etc/systemd/system/ollama.service.d
sudo bash -c 'cat > /etc/systemd/system/ollama.service.d/override.conf << EOF
[Service]
Environment="OLLAMA_HOST=0.0.0.0"
Environment="OLLAMA_KEEP_ALIVE=-1"
Environment="OLLAMA_NUM_PARALLEL=8"
EOF'
sudo systemctl daemon-reload
sudo systemctl enable ollama
sudo systemctl restart ollama
echo "[$(date)] Ollama 설치 및 시작 완료" | tee -a $LOG

# ── 3. qwen3-vl:8b 모델 다운로드 ─────────────────────────────────────────────
echo "[$(date)] qwen3-vl:8b 다운로드 중 (약 5GB, 시간 소요)..." | tee -a $LOG
sleep 5  # ollama 서비스 기동 대기
ollama pull qwen3-vl:8b 2>&1 | tail -5
echo "[$(date)] qwen3-vl:8b 다운로드 완료" | tee -a $LOG

# ── 4. Python 의존성 설치 ─────────────────────────────────────────────────────
echo "[$(date)] Python 의존성 설치 중..." | tee -a $LOG
sudo apt-get install -y python3-pip python3-venv poppler-utils git -qq 2>&1 | tail -3

PROJECT=/home/kjy/My_Digital_Persona_Own_LLM_Project
mkdir -p $PROJECT
cd $PROJECT

python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip -q
pip install -q ollama openai-whisper qdrant-client torch tqdm Pillow pdf2image \
    llama-index llama-index-vector-stores-qdrant llama-index-embeddings-huggingface \
    pymupdf olefile sentence-transformers gradio psutil
echo "[$(date)] Python 의존성 설치 완료" | tee -a $LOG

# ── 5. 완료 확인 ──────────────────────────────────────────────────────────────
echo "[$(date)] === 셋업 완료 ===" | tee -a $LOG
nvidia-smi 2>&1 | head -10 | tee -a $LOG
ollama list | tee -a $LOG
